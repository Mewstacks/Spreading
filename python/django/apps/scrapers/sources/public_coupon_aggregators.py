"""Public coupon catalogs used only as independent discovery evidence.

Both sites expose the coupon code, numeric benefit and validity in the public
HTML returned to an ordinary browser.  We never persist their outbound affiliate
redirect: Spreading must build the account owner's own affiliate URL later.

These are third-party claims.  A row from either source remains blocked until an
official observation, checkout validation, or another recent independent source
agrees on marketplace, code and numeric discount.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, time as datetime_time
from urllib.parse import urlsplit

import requests
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from apps.scrapers.coupon_rules import normalizar_regras_cupom, tem_restricao_publico
from apps.scrapers.cupom_extractor import codigo_plausivel

from .base import IngestedItem, SourceAdapter, normalizar_dinheiro


logger = logging.getLogger(__name__)
_TIMEOUT = (5, 20)
_PAUSA_S = 1.0
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_MARKETPLACES = ("amazon", "shopee")
_BIA_URL = "https://biagarimpa.com/cupons/{marketplace}"
_SPOT_URL = "https://cupomspot.com.br/cupons/{marketplace}"
_PRIMA_URL = "https://primaryca.com.br/cupons"

# Next.js RSC serializes each card as an escaped, flat JSON object.  The object
# currently has scalar/list/null fields only; requiring coupon_id and decoding
# each object independently makes a surrounding page redesign fail closed.
_BIA_COUPON = re.compile(
    r'\\"coupon\\":(\{\\"coupon_id\\".*?\})', re.S,
)
_LD_JSON = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_PERCENT = re.compile(r"(?<![\d.,])(\d{1,2}(?:[.,]\d{1,2})?)\s*%", re.I)
_MONEY_VALUE = r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
_MONEY = re.compile(rf"R\$\s*{_MONEY_VALUE}", re.I)
_MINIMUM = re.compile(
    rf"(?:acima de|a partir de|em compras? (?:acima )?de|m[ií]nim[oa](?: de)?|"
    rf"m[ií]n\.?|\bem)\s*"
    rf"R\$\s*{_MONEY_VALUE}", re.I,
)
_MAXIMUM = re.compile(
    rf"(?:limite|limitad[oa](?: a)?|m[aá]ximo)(?: de)?\s*R\$\s*{_MONEY_VALUE}",
    re.I,
)
_EXPLICIT_DATE = re.compile(
    r"(?<!\d)(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2}|\d{4}))?(?!\d)",
)


def _download(url):
    response = requests.get(url, timeout=_TIMEOUT, headers={"User-Agent": _UA})
    if response.status_code != 200:
        return ""
    # Both pages are UTF-8 but one currently omits a useful charset header.
    return response.content.decode("utf-8", errors="replace")


def _when(value, *, end_of_day=False):
    raw = str(value or "").strip()
    if not raw:
        return None
    if end_of_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        day = parse_date(raw)
        return (
            timezone.make_aware(datetime.combine(day, datetime_time.max))
            if day is not None else None
        )
    parsed = parse_datetime(raw)
    if parsed is not None:
        return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed
    day = parse_date(raw)
    if day is None:
        return None
    clock = datetime_time.max if end_of_day else datetime_time.min
    return timezone.make_aware(datetime.combine(day, clock))


def _discount(text):
    text = html.unescape(str(text or ""))
    percent = _PERCENT.search(text)
    if percent:
        value = float(percent.group(1).replace(",", "."))
        if 0 < value < 100:
            return "porcentagem", value
    money = _MONEY.search(text)
    if money:
        value = normalizar_dinheiro(money.group(1))
        if value > 0:
            return "fixo", value
    return "", 0.0


def _money_match(pattern, text):
    match = pattern.search(text or "")
    return normalizar_dinheiro(match.group(1)) if match else None


def _cents(value):
    try:
        return round(float(value) / 100, 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def _item(*, source, marketplace, external_id, code, discount_type, discount,
          description, observed_at, valid_from=None, valid_until=None,
          minimum=None, maximum=None, restricted=False, evidence=None):
    code = str(code or "").strip().upper()
    if not codigo_plausivel(code) or discount <= 0:
        return None
    if discount_type == "porcentagem" and discount >= 100:
        return None
    rules = normalizar_regras_cupom({
        "tipo_desconto": discount_type,
        "valor_desconto": discount,
        "valor_minimo": minimum,
        "desconto_maximo": maximum,
        "modo_resgate": "codigo",
        "escopo": description,
    }, external_id=external_id, codigo=code)
    label = (
        f"{discount:g}% OFF" if discount_type == "porcentagem"
        else f"R$ {discount:g} OFF"
    )
    proof = {
        "confianca_origem": "comunidade",
        "descricao": str(description or "")[:300],
        **(evidence or {}),
    }
    return IngestedItem(
        external_id=external_id[:160], marketplace=marketplace, source=source,
        kind="coupon", canonical_url="", title=f"Cupom {code} — {label}"[:255],
        coupon_code=code[:120], coupon_rules=rules, content_type="voucher",
        starts_at=valid_from, valid_until=valid_until, observed_at=observed_at,
        restricted=bool(restricted or tem_restricao_publico(description)),
        evidence=proof,
    )


def _bia_rows(body):
    for raw in _BIA_COUPON.findall(body or ""):
        try:
            row = json.loads(raw.replace(r'\"', '"'))
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict):
            yield row


def _walk_offers(value):
    if isinstance(value, dict):
        if value.get("@type") == "Offer" and value.get("couponCode"):
            yield value
        for child in value.values():
            yield from _walk_offers(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_offers(child)


def _spot_rows(body):
    for raw in _LD_JSON.findall(body or ""):
        try:
            block = json.loads(html.unescape(raw))
        except (TypeError, ValueError):
            continue
        yield from _walk_offers(block)


def _prima_sections(body):
    """Coupon dictionaries grouped by the marketplace section rendered by RSC."""
    decoded = (body or "").replace(r'\"', '"')
    markers = list(re.finditer(r'"section","(amazon|mercadolivre|shopee)"', decoded))
    for index, marker in enumerate(markers):
        marketplace = marker.group(1)
        end = markers[index + 1].start() if index + 1 < len(markers) else len(decoded)
        chunk = decoded[marker.start():end]
        for raw in re.findall(r'"coupon":(\{"id":".*?\})\}', chunk, re.S):
            try:
                row = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(row, dict):
                yield marketplace, row


def _explicitly_expired(description, observed_at):
    """Reject descriptions that still advertise a calendar date already past."""
    today = timezone.localtime(observed_at).date()
    dates = []
    for day, month, year in _EXPLICIT_DATE.findall(description or ""):
        year = int(year) if year else today.year
        if year < 100:
            year += 2000
        try:
            dates.append(datetime(year, int(month), int(day)).date())
        except ValueError:
            continue
    return bool(dates and max(dates) < today)


def _marketplace_host(url, marketplace):
    try:
        host = (urlsplit(str(url or "")).hostname or "").casefold()
    except ValueError:
        return False
    domain = f"{marketplace}.com.br"
    return host == domain or host.endswith(f".{domain}")


class _PublicCatalogSource(SourceAdapter):
    marketplace = "multiloja"
    requires_chromium = False
    inventario_completo = False
    url_template = ""

    def __init__(self):
        self.last_metrics = {}
        self.last_health_status = "unknown"

    def _download(self, marketplace):
        return _download(self.url_template.format(marketplace=marketplace))

    def _parse(self, marketplace, body, observed_at):
        raise NotImplementedError

    def discover_coupons(self, marketplaces=None, **kwargs):
        selected = set(marketplaces or _MARKETPLACES)
        targets = [m for m in _MARKETPLACES if m in selected]
        observed_at = timezone.now()
        read = failed = rows_seen = 0
        rejected = defaultdict(int)
        seen = set()
        for index, marketplace in enumerate(targets):
            if index:
                time.sleep(_PAUSA_S)
            try:
                body = self._download(marketplace)
            except requests.RequestException as exc:
                logger.info("%s/%s unavailable (%s).", self.slug, marketplace,
                            type(exc).__name__)
                failed += 1
                continue
            if not body:
                failed += 1
                continue
            read += 1
            for item, reason in self._parse(marketplace, body, observed_at):
                rows_seen += 1
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
            "rows_seen": rows_seen, "cupons": len(seen),
            "rejected_by_reason": dict(sorted(rejected.items())),
            "complete": False,
        }

    def discover_offers(self, **kwargs):
        return []

    def healthcheck(self):
        return {
            "ok": self.last_health_status == "healthy",
            "status": self.last_health_status,
            "metrics": self.last_metrics,
        }


class BiaGarimpaCouponsSource(_PublicCatalogSource):
    slug = "bia-garimpa-cupons"
    name = "Bia Garimpa — catálogo público de cupons"
    url_template = _BIA_URL

    def _parse(self, marketplace, body, observed_at):
        for row in _bia_rows(body):
            if str(row.get("retailer") or "").casefold() != marketplace:
                yield None, "wrong_marketplace"
                continue
            kind = str(row.get("discount_type") or "")
            if kind == "percentage":
                discount_type = "porcentagem"
                try:
                    discount = float(row.get("discount_value") or 0)
                except (TypeError, ValueError):
                    discount = 0.0
            elif kind == "fixed":
                discount_type = "fixo"
                try:
                    discount = round(float(row.get("discount_value") or 0) / 100, 2)
                except (TypeError, ValueError):
                    discount = 0.0
            else:
                yield None, "unsupported_discount"
                continue
            valid_until = _when(row.get("valid_until"))
            if valid_until and valid_until < observed_at:
                yield None, "expired"
                continue
            categories = row.get("categories")
            description = ", ".join(categories) if isinstance(categories, list) else ""
            if row.get("first_purchase_only"):
                description = f"{description}; primeira compra".strip("; ")
            code = str(row.get("code") or "").strip().upper()
            external_id = f"bia:{marketplace}:{row.get('coupon_id') or code}"
            item = _item(
                source=self.slug, marketplace=marketplace, external_id=external_id,
                code=code, discount_type=discount_type, discount=discount,
                description=description, observed_at=observed_at,
                valid_from=_when(row.get("valid_from")), valid_until=valid_until,
                minimum=_cents(row.get("min_purchase")),
                maximum=_cents(row.get("max_discount")),
                restricted=bool(row.get("first_purchase_only")),
                evidence={
                    "transport": "bia-garimpa-next-rsc",
                    "validation_score": row.get("validation_score"),
                    "feedback_positive": row.get("feedback_positive"),
                    "feedback_total": row.get("feedback_total"),
                },
            )
            yield item, "" if item else "invalid_code_or_discount"


class CupomSpotCouponsSource(_PublicCatalogSource):
    slug = "cupomspot-cupons"
    name = "CupomSpot — catálogo público verificado"
    url_template = _SPOT_URL

    def _parse(self, marketplace, body, observed_at):
        for row in _spot_rows(body):
            code = str(row.get("couponCode") or "").strip().upper()
            description = " ".join(filter(None, (
                str(row.get("discount") or ""), str(row.get("name") or ""),
                str(row.get("description") or ""),
            )))
            discount_type, discount = _discount(description)
            valid_until = _when(row.get("priceValidUntil"), end_of_day=True)
            if valid_until and valid_until < observed_at:
                yield None, "expired"
                continue
            external_id = f"cupomspot:{marketplace}:{code}"
            item = _item(
                source=self.slug, marketplace=marketplace, external_id=external_id,
                code=code, discount_type=discount_type, discount=discount,
                description=description, observed_at=observed_at,
                valid_until=valid_until,
                minimum=_money_match(_MINIMUM, description),
                maximum=_money_match(_MAXIMUM, description),
                evidence={"transport": "cupomspot-schema-org"},
            )
            yield item, "" if item else "invalid_code_or_discount"


class PrimaRycaCouponsSource(_PublicCatalogSource):
    slug = "prima-ryca-cupons"
    name = "Prima Ryca — radar público de cupons"
    url_template = _PRIMA_URL

    def _download(self, marketplace):
        # One page contains the marketplace sections; the base class cache is the
        # adapter instance, so avoid two 1.3 MB transfers in the same collection.
        if not hasattr(self, "_body_for_cycle"):
            self._body_for_cycle = _download(self.url_template)
        return self._body_for_cycle

    def discover_coupons(self, marketplaces=None, **kwargs):
        try:
            yield from super().discover_coupons(marketplaces=marketplaces, **kwargs)
        finally:
            self.__dict__.pop("_body_for_cycle", None)

    def _parse(self, marketplace, body, observed_at):
        for section, row in _prima_sections(body):
            if section != marketplace:
                continue
            code = str(row.get("code") or "").strip().upper()
            description = str(row.get("description") or "").strip()
            redeem_url = str(
                row.get("redeemUrl") or row.get("eligibleProductsUrl") or ""
            ).strip()
            # A measured page bug places old Mercado Livre cards after the Shopee
            # section.  A URL for another marketplace is therefore a hard reject;
            # URL-less rows can still be Shopee codes explicitly grouped there.
            if redeem_url and not _marketplace_host(redeem_url, marketplace):
                yield None, "wrong_marketplace_url"
                continue
            if _explicitly_expired(description, observed_at):
                yield None, "expired_in_description"
                continue
            kind = str(row.get("discountType") or "").casefold()
            try:
                discount = float(str(row.get("discountValue") or "").replace(",", "."))
            except (TypeError, ValueError):
                discount = 0.0
            discount_type = (
                "porcentagem" if kind == "percent" else "fixo" if kind == "fixed" else ""
            )
            valid_until = _when(row.get("expiresAt"))
            if valid_until and valid_until < observed_at:
                yield None, "expired"
                continue
            external_id = f"prima-ryca:{marketplace}:{row.get('id') or code}"
            item = _item(
                source=self.slug, marketplace=marketplace, external_id=external_id,
                code=code, discount_type=discount_type, discount=discount,
                description=description, observed_at=observed_at,
                valid_until=valid_until,
                minimum=_money_match(_MINIMUM, description),
                maximum=_money_match(_MAXIMUM, description),
                evidence={
                    "transport": "prima-ryca-next-rsc",
                    "scope_type_claim": str(row.get("scopeType") or "")[:40],
                    "has_public_marketplace_link": bool(redeem_url),
                },
            )
            yield item, "" if item else "invalid_code_or_discount"
