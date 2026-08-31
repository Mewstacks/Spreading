"""Radar de cupons digitaveis publicados no catalogo publico do Pelando.

O endpoint de busca de lojas e publico e nao exige sessao. A coleta e pequena
(uma consulta serial por marketplace), usa identificacao honesta e nunca segue
nem persiste ``redirectUrl``: esse link pertence ao programa de afiliados do
agregador. Como toda alegacao comunitaria, o resultado continua sujeito aos
gates de corroboracao/checkout antes de ser publicavel.
"""
from collections import defaultdict
import html
import logging
import re
import time
from urllib.parse import urlsplit, urlunsplit

import requests
from django.utils import timezone

from apps.scrapers.coupon_rules import normalizar_regras_cupom, tem_restricao_publico
from .base import IngestedItem, SourceAdapter, normalizar_dinheiro


logger = logging.getLogger(__name__)

_ENDPOINT = "https://api-web.pelando.com.br/stores/search"
_TIMEOUT = (5, 20)
_PAUSA_S = 1.25
_UA = "SpreadingCouponRadar/1.0 (+https://spreading.com.br)"

# Termo de busca, slug exato devolvido pela API e marketplace interno. Conferir
# o slug exato impede que uma busca aproximada (ex.: Amazon -> Amaro) contamine
# o catalogo.
LOJAS = {
    "amazon": ("amazon", "amazon"),
    "mercadolivre": ("mercado livre", "mercado-livre"),
    "shopee": ("shopee", "shopee"),
}
_DOMINIOS_OFICIAIS = {
    "amazon": ("amazon.com.br",),
    "mercadolivre": ("mercadolivre.com.br", "mercadolivre.com"),
    "shopee": ("shopee.com.br",),
}

_TAG = re.compile(r"<[^>]+>")
_CODE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{2,39}$")
_PERCENT = re.compile(r"(?<![\d.,])(\d{1,2}(?:[.,]\d{1,2})?)\s*%", re.I)
_MONEY_VALUE = r"(\d{1,3}(?:\.\d{3})+(?:,\d{2})?|\d+(?:,\d{2})?)"
_MONEY = re.compile(rf"R\$\s*{_MONEY_VALUE}", re.I)
_MINIMUM = re.compile(
    rf"(?:a partir de|acima de|m[ií]nim[oa](?:\s+de)?|compras? de)\s*"
    rf"R\$\s*{_MONEY_VALUE}", re.I,
)
_MAXIMUM = re.compile(
    rf"(?:desconto m[aá]ximo(?: de)?|limitad[oa]\s+a(?:t[eé])?|teto de|limite de)\s*"
    rf"R\$\s*{_MONEY_VALUE}", re.I,
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


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return float(str(value or "").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _official_url(marketplace, value):
    """Retem somente HTTP(S) no host oficial e remove tracking da query."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold().rstrip(".")
    allowed = _DOMINIOS_OFICIAIS.get(marketplace, ())
    if parsed.scheme not in {"http", "https"} or not any(
        host == domain or host.endswith(f".{domain}") for domain in allowed
    ):
        return ""
    # A evidencia precisa do caminho da campanha/produto, nao de ref/click IDs.
    return urlunsplit(("https", host, parsed.path or "/", "", ""))


def _discount(row, text):
    percent = _number(row.get("discountPercentage"))
    if 0 < percent < 100:
        return "porcentagem", percent

    for key in ("discountFixed", "discountValue", "discountAmount"):
        fixed = _number(row.get(key))
        if fixed > 0:
            return "fixo", fixed

    match = _PERCENT.search(text)
    if match:
        percent = _number(match.group(1))
        if 0 < percent < 100:
            return "porcentagem", percent
    match = _MONEY.search(text)
    if match:
        fixed = _money(match.group(1))
        if fixed > 0:
            return "fixo", fixed
    return "", 0.0


def _parse_coupon(marketplace, row, observed_at):
    if not isinstance(row, dict) or str(row.get("status") or "").casefold() != "active":
        return None, "inactive"

    code = str(row.get("couponCode") or "").strip().upper()
    if not _CODE.fullmatch(code):
        return None, "missing_or_invalid_code"
    if code in _PLACEHOLDERS:
        return None, "placeholder_code"

    title = _text(row.get("title"))
    description = _text(row.get("description"))
    rules_text = _text(row.get("rulesDescription"))
    combined = " ".join(part for part in (title, description, rules_text) if part)
    discount_type, discount = _discount(row, combined)
    if not discount:
        return None, "missing_discount"

    minimum = _MINIMUM.search(combined)
    maximum = _MAXIMUM.search(combined)
    coupon_id = str(row.get("id") or "").strip()
    external_id = f"pelando:{marketplace}:{coupon_id or code}"
    rules = normalizar_regras_cupom({
        "tipo_desconto": discount_type,
        "valor_desconto": discount,
        "valor_minimo": _money(minimum.group(1)) if minimum else None,
        "desconto_maximo": _money(maximum.group(1)) if maximum else None,
        "modo_resgate": "codigo",
        "escopo": rules_text or description or title,
    }, external_id=external_id, codigo=code)
    official = _official_url(marketplace, row.get("sourceUrl"))
    label = (
        f"{discount:g}% OFF" if discount_type == "porcentagem"
        else f"R$ {discount:g} OFF"
    )
    return IngestedItem(
        external_id=external_id[:160], marketplace=marketplace,
        source="pelando-cupons", kind="coupon", canonical_url="",
        title=f"Cupom {code} — {label}"[:255], coupon_code=code[:120],
        coupon_rules=rules, content_type="voucher",
        restricted=tem_restricao_publico(combined), observed_at=observed_at,
        evidence={
            "transport": "pelando-public-store-search",
            "coupon_id": coupon_id,
            "descricao": (rules_text or description or title)[:300],
            "official_source_url": official,
            "temperature": _number(row.get("temperature")),
            "comment_count": int(_number(row.get("commentCount"))),
            "confianca_origem": "comunidade",
        },
    ), ""


class PelandoCouponsSource(SourceAdapter):
    slug = "pelando-cupons"
    marketplace = "multiloja"
    name = "Pelando — cupons por loja"
    requires_chromium = False
    inventario_completo = False

    def __init__(self):
        self.last_metrics = {}
        self.last_health_status = "unknown"

    def _download(self, term):
        response = requests.get(
            _ENDPOINT, params={"term": term}, timeout=_TIMEOUT,
            headers={"User-Agent": _UA, "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _exact_store(payload, expected_slug):
        data = payload.get("data") if isinstance(payload, dict) else None
        stores = data.get("stores") if isinstance(data, dict) else None
        if not isinstance(stores, list):
            return None
        return next((row for row in stores if isinstance(row, dict) and
                     str(row.get("slug") or "").casefold() == expected_slug), None)

    def discover_coupons(self, marketplaces=None, **kwargs):
        selected = set(marketplaces or LOJAS)
        targets = [(marketplace, *LOJAS[marketplace]) for marketplace in LOJAS
                   if marketplace in selected]
        observed_at = timezone.now()
        read = failed = coupons_seen = 0
        seen = set()
        rejected = defaultdict(int)

        for index, (marketplace, term, expected_slug) in enumerate(targets):
            if index:
                time.sleep(_PAUSA_S)
            try:
                payload = self._download(term)
            except (requests.RequestException, ValueError) as exc:
                logger.info("Pelando/%s indisponivel (%s).", marketplace,
                            type(exc).__name__)
                failed += 1
                continue
            store = self._exact_store(payload, expected_slug)
            if store is None:
                rejected["exact_store_missing"] += 1
                failed += 1
                continue
            coupons = store.get("coupons")
            if not isinstance(coupons, list):
                rejected["invalid_schema"] += 1
                failed += 1
                continue
            read += 1
            for row in coupons:
                coupons_seen += 1
                item, reason = _parse_coupon(marketplace, row, observed_at)
                if item is None:
                    rejected[reason or "invalid"] += 1
                    continue
                key = (item.marketplace, item.coupon_code)
                if key in seen:
                    rejected["duplicate_code"] += 1
                    continue
                seen.add(key)
                yield item

        self.last_health_status = "healthy" if read == len(targets) else (
            "partial" if read else "degraded"
        )
        self.last_metrics = {
            "lojas_lidas": read, "lojas_falhas": failed,
            "cupons_vistos": coupons_seen, "cupons": len(seen),
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
