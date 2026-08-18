"""Fontes da Shopee: ofertas de produto e campanhas, ambas por API assinada.

Dois adaptadores, não um, porque as duas saídas têm regras de vida diferentes:

  - ``shopee-offers`` (``productOfferV2``) alimenta ``Produto``. É catálogo: preço
    muda o tempo todo e a ausência de um item numa coleta não significa nada.
  - ``shopee-campaigns`` (``shopeeOfferV2``) alimenta ``CupomNormalizado``. É
    promoção com janela de vigência, e é o que o aviso de cupom publica.

Nenhum dos dois usa Chromium (``requires_chromium`` fica False, que é o default), e
essa é a diferença que importa: eles não entram na fila do navegador que hoje
represa o funil de cupons do Mercado Livre.

Sobre "cupom" na Shopee: a API de afiliados NÃO expõe endpoint de voucher. O que
existe são campanhas — nome, comissão e janela — com link comissionado próprio. O
adaptador as projeta como cupom de ATIVAÇÃO (``modo_resgate="ativacao"``), nunca de
código: anunciar um código que a pessoa não consegue digitar no checkout é
exatamente o tipo de promessa quebrada que derruba a confiança de um grupo.
"""
import logging
from datetime import datetime, timezone as dt_timezone

from django.utils import timezone

from apps.scrapers.coupon_rules import normalizar_regras_cupom, tem_restricao_publico
from apps.scrapers.shopee import (
    ShopeeConfigError, ShopeeError, credenciais_da_integracao, listar_campanhas,
    listar_produtos,
)
from .base import IngestedItem, SourceAdapter

logger = logging.getLogger(__name__)

# Janela curta = campanha relâmpago. O mesmo critério já usado na Awin
# (`awin.coletar_ofertas`), para que "relâmpago" signifique a mesma coisa em todas
# as lojas — senão o selo vira enfeite e o usuário aprende a ignorá-lo.
HORAS_RELAMPAGO = 6
SEGUNDOS_UM_DIA = 86_400


def _integracao(owner):
    from apps.scrapers.models import IntegracaoAfiliado

    if owner is None:
        return None
    return IntegracaoAfiliado.objects.filter(
        owner=owner, provedor="shopee", habilitada=True,
    ).first()


def _instante(valor):
    """Epoch em segundos -> datetime ciente. A Shopee devolve inteiros."""
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return None
    if numero <= 0:
        return None
    return datetime.fromtimestamp(numero, tz=dt_timezone.utc)


def _decimal(valor):
    try:
        return round(float(str(valor).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return 0.0


class _ShopeeSourceBase(SourceAdapter):
    marketplace = "shopee"
    requires_chromium = False

    def __init__(self):
        self.last_metrics = {}
        self.last_health_status = "unknown"

    def _credenciais(self, owner):
        integracao = _integracao(owner)
        if not integracao:
            # Sem conta conectada não é falha: é fonte não configurada. Marcar como
            # erro acumularia falhas_consecutivas e o circuit breaker bloquearia a
            # fonte para quem conectasse depois.
            raise ShopeeConfigError("Conta de afiliado da Shopee não conectada.")
        return credenciais_da_integracao(integracao)

    def _registrar_vazio(self, motivo):
        self.last_health_status = "healthy_empty" if motivo == "vazio" else "degraded"
        self.last_metrics = {"complete": False, "reason": motivo}

    def healthcheck(self):
        return {"ok": True, "status": "ok"}


class ShopeeOffersSource(_ShopeeSourceBase):
    slug = "shopee-offers"
    name = "Shopee — ofertas de produto (API)"

    def discover_offers(self, owner=None, termos=None, **kwargs):
        try:
            app_id, secret = self._credenciais(owner)
        except ShopeeConfigError:
            self._registrar_vazio("sem_credencial")
            return
        termos = [t for t in (termos or [""]) if t is not None][:8] or [""]
        agora = timezone.now()
        vistos = set()
        linhas = 0
        completa = True
        for termo in termos:
            try:
                nos, termo_completo = listar_produtos(
                    app_id=app_id, secret=secret, keyword=str(termo or ""),
                )
            except ShopeeError as exc:
                # Parcial preserva o catálogo anterior (ver registry/persistence):
                # uma coleta interrompida jamais pode autorizar expirar itens.
                logger.warning("Shopee ofertas: %s", exc.public_message)
                completa = False
                continue
            completa = completa and termo_completo
            for no in nos:
                item_id = str(no.get("itemId") or "").strip()
                loja_id = str(no.get("shopId") or "").strip()
                if not item_id or not loja_id:
                    continue
                chave = f"{loja_id}_{item_id}"
                if chave in vistos:
                    continue
                destino = str(no.get("productLink") or no.get("offerLink") or "").strip()
                if not destino.startswith("https://"):
                    continue
                preco = _decimal(no.get("priceMin"))
                if preco <= 0:
                    continue
                # `priceDiscountRate` vem em pontos percentuais. Sem preço "de", ele
                # é a única prova de desconto, e a referência é derivada dele em vez
                # de inventada — quem não tiver desconto declarado entra com
                # referência zero e não passa pelos gates de "ótima promoção".
                desconto = _decimal(no.get("priceDiscountRate"))
                referencia = (
                    round(preco / (1 - desconto / 100), 2)
                    if 0 < desconto < 95 else 0.0
                )
                vistos.add(chave)
                linhas += 1
                yield IngestedItem(
                    external_id=chave[:160], marketplace="shopee", source=self.slug,
                    kind="offer", canonical_url=destino[:1000],
                    title=str(no.get("productName") or "")[:255],
                    current_price=preco, reference_price=referencia,
                    image_url=str(no.get("imageUrl") or "")[:1000],
                    observed_at=agora,
                    evidence={
                        "transport": "shopee-affiliate-api",
                        "shop_name": str(no.get("shopName") or "")[:180],
                        "commission_rate": _decimal(no.get("commissionRate")),
                        "sales": int(_decimal(no.get("sales"))),
                        "rating": _decimal(no.get("ratingStar")),
                        "discount_rate": desconto,
                        "keyword": str(termo or ""),
                    },
                )
        self.last_health_status = "healthy" if linhas else "healthy_empty"
        self.last_metrics = {
            "rows": linhas,
            # `complete` só é verdadeiro quando NENHUM termo ficou pela metade. É
            # esta flag que autoriza projetar ausência; na dúvida, não autoriza.
            "complete": bool(completa),
            "keywords": len(termos),
        }

    def discover_coupons(self, **kwargs):
        return []


class ShopeeCampaignsSource(_ShopeeSourceBase):
    slug = "shopee-campaigns"
    name = "Shopee — campanhas e cupons (API)"

    def discover_offers(self, **kwargs):
        return []

    def discover_coupons(self, owner=None, **kwargs):
        try:
            app_id, secret = self._credenciais(owner)
        except ShopeeConfigError:
            self._registrar_vazio("sem_credencial")
            return
        try:
            nos, completa = listar_campanhas(app_id=app_id, secret=secret)
        except ShopeeError as exc:
            logger.warning("Shopee campanhas: %s", exc.public_message)
            self._registrar_vazio("degradado")
            return

        agora = timezone.now()
        linhas = 0
        for no in nos:
            destino = str(no.get("offerLink") or "").strip()
            nome = str(no.get("offerName") or "").strip()
            if not destino.startswith("https://") or not nome:
                continue
            inicio = _instante(no.get("periodStartTime"))
            fim = _instante(no.get("periodEndTime"))
            comissao = _decimal(no.get("commissionRate"))
            # A comissão da Shopee vem como fração (0.08) ou percentual (8),
            # conforme a campanha. Normaliza para ponto percentual antes de virar
            # "valor do desconto", senão 0,08% seria anunciado no lugar de 8%.
            if 0 < comissao <= 1:
                comissao = round(comissao * 100, 2)
            if comissao <= 0:
                # Sem valor comprovado o cupom seria descartado adiante por
                # `missing_discount`; melhor não persistir do que sujar o funil.
                continue
            relampago = bool(
                (inicio and fim and (fim - inicio).total_seconds() <= SEGUNDOS_UM_DIA)
                or (fim and agora <= fim <= agora + timezone.timedelta(
                    hours=HORAS_RELAMPAGO))
            )
            external_id = f"shopee:campanha:{no.get('offerType') or 'geral'}:{nome}"[:160]
            regras = normalizar_regras_cupom({
                "tipo_desconto": "percentual",
                "valor_desconto": comissao,
                "modo_resgate": "ativacao",
                "escopo": str(no.get("offerType") or ""),
                "dia_inicio": inicio.isoformat() if inicio else "",
                "dia_fim": fim.isoformat() if fim else "",
            }, external_id=external_id, codigo="")
            linhas += 1
            yield IngestedItem(
                external_id=external_id, marketplace="shopee", source=self.slug,
                kind="coupon", canonical_url=destino[:1000], title=nome[:255],
                coupon_code="", coupon_rules=regras, content_type="promotion",
                starts_at=inicio, valid_until=fim,
                image_url=str(no.get("imageUrl") or "")[:1000],
                restricted=tem_restricao_publico(nome),
                flash=relampago, observed_at=agora,
                evidence={
                    "transport": "shopee-affiliate-api",
                    "offer_type": str(no.get("offerType") or "")[:80],
                    "commission_rate": comissao,
                },
            )
        self.last_health_status = "healthy" if linhas else "healthy_empty"
        self.last_metrics = {"rows": linhas, "complete": bool(completa)}
