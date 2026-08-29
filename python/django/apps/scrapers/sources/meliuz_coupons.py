"""Radar de codigos publicados nas paginas publicas do Meliuz.

O Meliuz e uma fonte de descoberta, nao uma prova de que o codigo ainda reduz o
carrinho. Por isso os itens saem marcados como comunidade e continuam retidos
pelos gates de prontidao ate existir evidencia oficial ou validacao de checkout.

Os links de redirecionamento do agregador nunca sao persistidos: a afiliacao do
envio precisa pertencer ao usuario do Spreading.
"""
from __future__ import annotations

import html
import logging
import re
import time
from collections import defaultdict

import requests
from django.utils import timezone

from apps.scrapers.coupon_rules import normalizar_regras_cupom, tem_restricao_publico

from .base import IngestedItem, SourceAdapter, normalizar_dinheiro


logger = logging.getLogger(__name__)

_TIMEOUT = (5, 20)
_PAUSA_S = 1.5
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
LOJAS = {
    "amazon": "https://www.meliuz.com.br/desconto/cupom-desconto-amazon",
    "mercadolivre": "https://www.meliuz.com.br/desconto/cupom-desconto-mercado-livre",
    "shopee": "https://www.meliuz.com.br/desconto/cupom-shopee",
}

_CARD_START = re.compile(
    r'<div\s+class="[^"]*\bcpn-layout\b[^\"]*\boffer-cpn\b[^\"]*"(?P<attrs>[^>]*)>',
    re.I,
)
_ATTRIBUTE = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')
_BENEFIT = re.compile(
    r'offer-cpn__offer-summary[^>]*>.*?<strong[^>]*>(.*?)</strong>', re.I | re.S,
)
_RULES = re.compile(r'cpn-layout__rules[^>]*>(.*?)</div>', re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_CODE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{2,39}$")
_PERCENT = re.compile(r"(?<![\d.,])(\d{1,2}(?:[.,]\d{1,2})?)\s*%", re.I)
_MONEY_VALUE = r"(\d{1,3}(?:\.\d{3})+(?:,\d{2})?|\d+(?:,\d{2})?)"
_MONEY = re.compile(rf"R\$\s*{_MONEY_VALUE}", re.I)
_MINIMUM = re.compile(
    rf"(?:a partir de|acima de|m[ií]nim[oa](?:\s+de)?|compras? de)\s*R\$\s*{_MONEY_VALUE}",
    re.I,
)
_MAXIMUM = re.compile(
    rf"(?:desconto m[aá]ximo(?: de)?|limitad[oa]\s+a(?:t[eé])?|limite de)\s*R\$\s*{_MONEY_VALUE}",
    re.I,
)
_PLACEHOLDERS = frozenset({
    "CUPOMNOLINK", "CUPOMAQUI", "DESCONTOAQUI", "DESCONTONOLINK",
    "GARANTACUPOM", "PEGUECUPOM", "USECUPOM", "MAISCUPONS",
    "RESGATENOLINK", "PEGUEAQUI", "ATIVEAQUI", "VEJANOLINK",
})


def _text(value):
    return " ".join(_TAG.sub(" ", html.unescape(str(value or ""))).split())


def _money(value):
    raw = str(value or "").strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
        raw = raw.replace(".", "")
    return normalizar_dinheiro(raw) if raw else 0.0


def _discount(text):
    percent = _PERCENT.search(text or "")
    if percent:
        value = float(percent.group(1).replace(",", "."))
        if 0 < value < 100:
            return "porcentagem", value
    money = _MONEY.search(text or "")
    if money:
        value = _money(money.group(1))
        if value > 0:
            return "fixo", value
    return "", 0.0


def _active_html(body):
    """Remove a secao que o proprio site identifica como expirada."""
    folded = (body or "").casefold()
    positions = [
        pos for marker in ("cupons expirados", "cupom expirado")
        if (pos := folded.find(marker)) >= 0
    ]
    return body[:min(positions)] if positions else body


def _card_blocks(body):
    body = _active_html(body)
    starts = list(_CARD_START.finditer(body or ""))
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        attrs = {
            key.casefold(): html.unescape(value)
            for key, value in _ATTRIBUTE.findall(match.group("attrs") or "")
        }
        yield attrs, body[match.end():end]


def _parse_card(marketplace, attrs, body, observed_at):
    code = str(attrs.get("data-offer-code") or "").strip().upper()
    if not _CODE.fullmatch(code):
        return None, "invalid_code"
    if code in _PLACEHOLDERS:
        return None, "placeholder_code"

    title = _text(attrs.get("data-offer-title"))
    benefit_match = _BENEFIT.search(body or "")
    rules_match = _RULES.search(body or "")
    benefit = _text(benefit_match.group(1)) if benefit_match else ""
    rules_text = _text(rules_match.group(1)) if rules_match else ""
    discount_type, discount = _discount(f"{benefit} {title} {rules_text}")
    if not discount:
        return None, "missing_discount"

    minimum = _MINIMUM.search(f"{title} {rules_text}")
    maximum = _MAXIMUM.search(f"{title} {rules_text}")
    offer_id = str(attrs.get("data-offer-id") or "").strip()
    external_id = f"meliuz:{marketplace}:{offer_id or code}"
    normalized = normalizar_regras_cupom({
        "tipo_desconto": discount_type,
        "valor_desconto": discount,
        "valor_minimo": _money(minimum.group(1)) if minimum else None,
        "desconto_maximo": _money(maximum.group(1)) if maximum else None,
        "modo_resgate": "codigo",
        "escopo": rules_text or title,
    }, external_id=external_id, codigo=code)
    label = f"{discount:g}% OFF" if discount_type == "porcentagem" else f"R$ {discount:g} OFF"
    return IngestedItem(
        external_id=external_id[:160], marketplace=marketplace,
        source="meliuz-cupons", kind="coupon", canonical_url="",
        title=f"Cupom {code} — {label}"[:255], coupon_code=code[:120],
        coupon_rules=normalized, content_type="voucher",
        restricted=tem_restricao_publico(f"{title} {rules_text}"),
        observed_at=observed_at,
        evidence={
            "transport": "meliuz-public-html",
            "offer_id": offer_id,
            "descricao": (rules_text or title)[:300],
            "confianca_origem": "comunidade",
        },
    ), ""


class MeliuzCouponsSource(SourceAdapter):
    slug = "meliuz-cupons"
    marketplace = "multiloja"
    name = "Meliuz — cupons por loja"
    requires_chromium = False
    inventario_completo = False

    def __init__(self):
        self.last_metrics = {}
        self.last_health_status = "unknown"

    def _download(self, url):
        response = requests.get(url, timeout=_TIMEOUT, headers={"User-Agent": _UA})
        if response.status_code != 200:
            return ""
        return response.text or ""

    def discover_coupons(self, marketplaces=None, **kwargs):
        selected = set(marketplaces or LOJAS)
        targets = [(marketplace, url) for marketplace, url in LOJAS.items()
                   if marketplace in selected]
        observed_at = timezone.now()
        seen = set()
        read = failed = cards_seen = 0
        rejected = defaultdict(int)
        for index, (marketplace, url) in enumerate(targets):
            if index:
                time.sleep(_PAUSA_S)
            try:
                body = self._download(url)
            except requests.RequestException as exc:
                logger.info("Meliuz/%s indisponivel (%s).", marketplace, type(exc).__name__)
                failed += 1
                continue
            if not body:
                failed += 1
                continue
            read += 1
            for attrs, block in _card_blocks(body):
                cards_seen += 1
                item, reason = _parse_card(marketplace, attrs, block, observed_at)
                if item is None:
                    rejected[reason or "invalid"] += 1
                    continue
                key = (item.marketplace, item.coupon_code)
                if key in seen:
                    rejected["duplicate_code"] += 1
                    continue
                seen.add(key)
                yield item

        self.last_health_status = "healthy" if read else "degraded"
        self.last_metrics = {
            "lojas_lidas": read, "lojas_falhas": failed,
            "cards_seen": cards_seen, "cupons": len(seen),
            "rejected_by_reason": dict(sorted(rejected.items())),
            "complete": False,
        }

    def discover_offers(self, **kwargs):
        return []

    def healthcheck(self):
        return {
            "ok": self.last_health_status == "healthy",
            "status": self.last_health_status,
            "metrics": dict(self.last_metrics),
        }
