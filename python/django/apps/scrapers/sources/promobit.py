"""Cupons das páginas públicas de loja do Promobit, pelo schema.org que eles publicam.

Por que esta fonte existe: até aqui o Mercado Livre dependia de UM site de terceiro
(`afiliadosmercadolivre.github.io`) para todo cupom. Fornecedor único para a principal
linha de produto. Aqui entra a segunda fonte, e ela não custa navegador nem credencial.

**De onde os dados saem.** Cada página `/cupons/loja/<loja>/` traz um bloco
`application/ld+json` com um `ItemList` de `Offer` contendo `discountCode`, `name`,
`description` e `seller`. Isso é schema.org — a superfície que o próprio site publica
para máquinas lerem, e o contrato mais estável que existe ali: muda muito menos que o
DOM e infinitamente menos que o estado interno do React.

**O que a política deles permite.** O `robots.txt` do Promobit desautoriza `/api/*` e
`/buscar*` explicitamente, e deixa as páginas de cupom por loja liberadas. Este
adaptador lê **apenas** `/cupons/loja/<loja>/`, com pausa entre requisições. A API
interna deles não é tocada — foi fechada por decisão do site, e contorná-la seria
ignorar uma política declarada.

**Confiança.** Cupom de comunidade é ALEGAÇÃO. Mais de 95% do que o Promobit publica
vem de usuários, validado por moderação humana — o que é bom, mas não é a mesma coisa
que ter visto o desconto acontecer. Por isso estes cupons nascem com precedência baixa
(ver `_SOURCE_PRECEDENCE` em `persistence.py`): eles somam evidência e corroboram o
que outra fonte já viu, mas não deveriam, sozinhos, mandar um influenciador anunciar
um código para o grupo dele.
"""
import html
import json
import logging
import re
import time

import requests
from django.utils import timezone

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
            for oferta in _ofertas(corpo):
                codigo = _texto(oferta.get("discountCode")).upper()
                if not _codigo_valido(codigo):
                    # Sem código digitável não há o que anunciar: a URL de redirect do
                    # Promobit levaria o clique (e a comissão) para eles, não para o
                    # usuário. E frase no lugar do código ("RESGATE NO PRODUTO", visto
                    # na fonte real) faria o grupo digitar algo que não existe.
                    continue
                nome = _texto(oferta.get("name"))
                descricao = _texto(oferta.get("description"))
                tipo, valor = _desconto(f"{nome} {descricao}")
                if not valor:
                    # Sem valor comprovado o cupom seria descartado adiante por
                    # `missing_discount`; melhor não sujar o funil.
                    continue
                chave = f"promobit:{marketplace}:{codigo}"
                if chave in vistos:
                    continue
                vistos.add(chave)
                regras = normalizar_regras_cupom({
                    "tipo_desconto": tipo,
                    "valor_desconto": valor,
                    "modo_resgate": "codigo",
                    "escopo": descricao,
                }, external_id=chave, codigo=codigo)
                yield IngestedItem(
                    external_id=chave[:160], marketplace=marketplace,
                    source=self.slug, kind="coupon",
                    # O destino é a loja, nunca o redirect do Promobit.
                    canonical_url="", title=(nome or f"Cupom {codigo}")[:255],
                    coupon_code=codigo[:120], coupon_rules=regras,
                    content_type="voucher",
                    restricted=tem_restricao_publico(f"{nome} {descricao}"),
                    observed_at=agora,
                    evidence={
                        "transport": "promobit-schema-org",
                        "loja": slug_loja,
                        "descricao": descricao[:300],
                        # Alegação da comunidade, não observação nossa. Fica escrito.
                        "confianca_origem": "comunidade",
                    },
                )
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
