"""Cupons das páginas públicas de loja do Promobit.

Por que esta fonte existe: a página oficial de cupons da Amazon lista ~8 cards de
*ativação*. O volume de código digitável (o que Cuponomia/Promobit anunciam) mora
nas páginas `/cupons/loja/<loja>/` dos agregadores.

**De onde os dados saem.** A mesma página HTML que o robots.txt libera. O Next.js
embute `serverCoupons.coupons` em `__NEXT_DATA__` (dezenas de códigos). O bloco
schema.org `ItemList`/`Offer` é recorte menor — fica como fallback. Não há GET em
`/api/*` (Disallow).

**Destino.** Nunca o `/Redirect/cupom/` do Promobit: o clique e a comissão iriam
para eles. `canonical_url` vazio; o envio monta `?tag=` na Amazon ou o aviso ML.

**Precedência.** Continua baixa (`persistence._SOURCE_PRECEDENCE`): fonte oficial
da loja vence o mesmo código. Promobit sozinho agora lista — Telegram não.
"""
import html
import json
import logging
import re
import time

import requests
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.scrapers.coupon_rules import normalizar_regras_cupom, tem_restricao_publico
from .base import IngestedItem, SourceAdapter

logger = logging.getLogger(__name__)

BASE = "https://www.promobit.com.br/cupons/loja/"
_TIMEOUT = (5, 20)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
# Pausa entre lojas. A coleta não tem pressa e o site é de terceiro.
_PAUSA_S = 1.5

_LD_JSON = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S,
)
_NEXT_DATA = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S,
)
_STATUS_OK = frozenset({"APPROVED", "VERIFIED", "ACTIVE", "PUBLISHED"})
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")

# Lojas cujo cupom o Spreading consegue afiliar. Publicar cupom de loja que não
# comissiona seria trabalho para o influenciador e receita para outra pessoa.
LOJAS = {
    "amazon": "amazon",
    "mercado-livre": "mercadolivre",
    "shopee": "shopee",
}

_PERCENTUAL = re.compile(r"(\d{1,2})\s*%")
_REAIS = re.compile(r"R\$\s*([\d.]+,\d{2}|\d+)")

# Código que a pessoa digita no checkout: sem espaço, 3 a 30 caracteres, letras,
# números e os separadores que o varejo usa. O filtro existe porque a fonte real
# devolve, no mesmo campo `discountCode`, frases como "Resgate no produto" — que
# descrevem COMO usar e não são código nenhum. Publicar isso manda o grupo digitar
# uma frase no checkout e não funcionar; é a definição de cupom que queima confiança.
_CODIGO_OK = re.compile(r"^[A-Z0-9][A-Z0-9._-]{2,29}$")


def _codigo_valido(codigo: str) -> bool:
    if not _CODIGO_OK.match(codigo or ""):
        return False
    # Só letras e curto demais costuma ser palavra solta ("CUPOM", "OFERTA").
    return not (codigo.isalpha() and len(codigo) < 5)


def _texto(valor):
    return html.unescape(str(valor or "")).strip()


def _blocos(corpo):
    for bruto in _LD_JSON.findall(corpo or ""):
        try:
            yield json.loads(bruto)
        except (ValueError, TypeError):
            continue


def _ofertas(corpo):
    """As `Offer` dentro dos `ItemList` da página."""
    for bloco in _blocos(corpo):
        if not isinstance(bloco, dict) or bloco.get("@type") != "ItemList":
            continue
        for item in bloco.get("itemListElement") or []:
            if isinstance(item, dict) and item.get("@type") == "Offer":
                yield item


def _quando(valor):
    raw = str(valor or "").strip()
    if not raw:
        return None
    if re.search(r"[+-]\d{4}$", raw):
        raw = f"{raw[:-2]}:{raw[-2:]}"
    parsed = parse_datetime(raw)
    if parsed is None:
        return None
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _status_publicado(status) -> bool:
    s = str(status or "").strip().upper()
    return (not s) or s in _STATUS_OK


def _cupons_next(corpo):
    match = _NEXT_DATA.search(corpo or "")
    if not match:
        return
    try:
        data = json.loads(match.group(1))
    except (ValueError, TypeError):
        return
    if not isinstance(data, dict):
        return
    bloco = ((data.get("props") or {}).get("pageProps") or {}).get("serverCoupons") or {}
    cupons = bloco.get("coupons") if isinstance(bloco, dict) else None
    if not isinstance(cupons, list):
        return
    for row in cupons:
        if isinstance(row, dict):
            yield row


def _montar(marketplace, slug_loja, codigo, nome, descricao, validade, agora,
            transport):
    codigo = _texto(codigo).upper()
    if not _codigo_valido(codigo):
        return None
    tipo, valor = _desconto(f"{nome} {descricao}")
    if not valor:
        return None
    chave = f"promobit:{marketplace}:{codigo}"
    regras = normalizar_regras_cupom({
        "tipo_desconto": tipo,
        "valor_desconto": valor,
        "modo_resgate": "codigo",
        "escopo": descricao,
    }, external_id=chave, codigo=codigo)
    return IngestedItem(
        external_id=chave[:160], marketplace=marketplace,
        source="promobit-cupons", kind="coupon",
        canonical_url="", title=(nome or f"Cupom {codigo}")[:255],
        coupon_code=codigo[:120], coupon_rules=regras,
        content_type="voucher",
        restricted=tem_restricao_publico(f"{nome} {descricao}"),
        observed_at=agora, valid_until=validade,
        evidence={
            "transport": transport,
            "loja": slug_loja,
            "descricao": (descricao or "")[:300],
            "confianca_origem": "comunidade",
        },
    )


def _desconto(texto):
    """(tipo, valor) a partir do texto da oferta. Sem número, não há cupom."""
    achado = _PERCENTUAL.search(texto)
    if achado:
        valor = int(achado.group(1))
        # 100% não existe em cupom de varejo; é erro de parse ou promessa falsa.
        if 0 < valor < 100:
            return "porcentagem", float(valor)
    achado = _REAIS.search(texto)
    if achado:
        from .base import normalizar_dinheiro
        valor = normalizar_dinheiro(achado.group(1))
        if valor > 0:
            return "fixo", valor
    return "", 0.0


class PromobitSource(SourceAdapter):
    slug = "promobit-cupons"
    marketplace = "multiloja"
    name = "Promobit — cupons por loja (schema.org)"
    requires_chromium = False
    # Vitrine curada / prévia recente: recorte por construção, nunca inventário.
    inventario_completo = False

    def __init__(self):
        self.last_metrics = {}
        self.last_health_status = "unknown"

    def _baixar(self, slug_loja):
        if not _SLUG_OK.match(slug_loja):
            logger.warning("Slug de loja recusado: %r", str(slug_loja)[:40])
            return ""
        resposta = requests.get(
            f"{BASE}{slug_loja}/", timeout=_TIMEOUT, headers={"User-Agent": _UA},
        )
        if resposta.status_code != 200:
            return ""
        return resposta.text or ""

    def discover_coupons(self, lojas=None, **kwargs):
        alvos = {
            slug: mkt for slug, mkt in LOJAS.items()
            if not lojas or slug in set(lojas)
        }
        agora = timezone.now()
        vistos = set()
        lidas = falhas = 0

        for indice, (slug_loja, marketplace) in enumerate(alvos.items()):
            if indice:
                time.sleep(_PAUSA_S)
            try:
                corpo = self._baixar(slug_loja)
            except requests.RequestException as exc:
                falhas += 1
                logger.info("Promobit/%s indisponível (%s).",
                            slug_loja, type(exc).__name__)
                continue
            if not corpo:
                falhas += 1
                continue
            lidas += 1
            for row in _cupons_next(corpo):
                if not _status_publicado(row.get("couponStatusName")):
                    continue
                validade = _quando(row.get("couponUntil"))
                if validade and validade < agora:
                    continue
                nome = _texto(row.get("couponTitle") or row.get("couponDiscountShort"))
                descricao = _texto(" ".join(filter(None, [
                    _texto(row.get("couponDiscountValue")),
                    _texto(row.get("couponDiscountShort")),
                    _texto(row.get("couponDiscount")),
                    _texto(row.get("couponDiscountOn")),
                    _texto(row.get("couponInstructions")),
                ])))
                item = _montar(
                    marketplace, slug_loja, row.get("couponCode"), nome,
                    descricao, validade, agora, "promobit-next-data",
                )
                if item is None or item.external_id in vistos:
                    continue
                vistos.add(item.external_id)
                yield item
            for oferta in _ofertas(corpo):
                nome = _texto(oferta.get("name"))
                descricao = _texto(oferta.get("description"))
                item = _montar(
                    marketplace, slug_loja, oferta.get("discountCode"), nome,
                    descricao, None, agora, "promobit-schema-org",
                )
                if item is None or item.external_id in vistos:
                    continue
                vistos.add(item.external_id)
                yield item
        self.last_health_status = "healthy" if lidas else "degraded"
        self.last_metrics = {
            "lojas_lidas": lidas,
            "lojas_falhas": falhas,
            "cupons": len(vistos),
            # Nunca completo: é uma vitrine curada, não o inventário de uma loja.
            # Ausência aqui não prova que o cupom acabou e não pode expirar nada.
            "complete": False,
        }

    def discover_offers(self, **kwargs):
        return []

    def healthcheck(self):
        return {"ok": True, "status": "ok"}
