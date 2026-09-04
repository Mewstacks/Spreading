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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as datetime_time, timedelta
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
_DISCOUP_URL = "https://www.discoup.com/br/ofertas-cupom-de-desconto-shopee.html"
_DISCOUP_POPUP = "https://www.discoup.com/br/api/offers/popup-{offer_id}"
_DISCOUP_WORKERS = 4
_DISCOUP_CACHE_SECONDS = 30 * 60
_PROMOMIA_URL = "https://promomia.com.br/cupons/shopee"
_CUPONATION_URL = "https://www.cuponation.com.br/cupom-shopee"
_CASHBE_URL = "https://cashbe.com.br/loja--cupom-shopee/61a9f2a423a1580d54098bb910/"
_PEGUEI_BARATO_URL = "https://pegueibarato.com.br/cupom-desconto-amazon/"

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
_PERCENT_OFF = re.compile(
    r"(?<![\d.,])(\d{1,2}(?:[.,]\d{1,2})?)\s*%\s*(?:off|de desconto)", re.I,
)
_MONEY_OFF = re.compile(
    rf"R\$\s*{_MONEY_VALUE}\s*(?:off|de desconto)", re.I,
)
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
_DISCOUP_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)
_DISCOUP_CODE = re.compile(r"\bcopy:'([^']{4,120})'", re.I)
_NEXT_FLIGHT_SCRIPT = re.compile(
    r'<script>\s*(self\.__next_f\.push\(\[1,.*?)</script>', re.I | re.S,
)
_SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.I | re.S)
_CASHBE_CARD = re.compile(
    r'<div class="card coupons__card".*?'
    r'(?=<div class="card coupons__card"|<div class="coupons__pagination"|$)',
    re.I | re.S,
)
_CASHBE_TITLE = re.compile(
    r'<h3 class="card__name"[^>]*>(.*?)</h3>', re.I | re.S,
)
_CASHBE_CODE = re.compile(
    r'<div class="card__button-code"[^>]*>(.*?)</div>', re.I | re.S,
)
_CASHBE_SAVING = re.compile(rf"economize\s*R\$\s*{_MONEY_VALUE}", re.I)
_PEGUEI_CARD_START = re.compile(
    r'<div\s+id="([0-9a-f-]{36})"\s+class="coupon__item"[^>]*>', re.I,
)
_PEGUEI_TITLE = re.compile(
    r'<h3[^>]*data-field="coupon-title"[^>]*>(.*?)</h3>', re.I | re.S,
)
_PEGUEI_DESCRIPTION = re.compile(
    r'<div[^>]*data-field="description"[^>]*>(.*?)</div>', re.I | re.S,
)
_PEGUEI_CODE = re.compile(
    r'<span\s+class="coupon__code"[^>]*>(.*?)</span>', re.I | re.S,
)
_PEGUEI_DESTINATION = re.compile(
    r'<div[^>]*class="[^"]*coupon__action[^"]*"[^>]*>.*?'
    r'<a[^>]+href="([^"]+)"', re.I | re.S,
)
_HTML_TAG = re.compile(r"<[^>]+>")


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


def _discount(text, *, require_explicit_off=False):
    text = html.unescape(str(text or ""))
    percent = (_PERCENT_OFF if require_explicit_off else _PERCENT).search(text)
    if percent:
        value = float(percent.group(1).replace(",", "."))
        if 0 < value < 100:
            return "porcentagem", value
    money = (_MONEY_OFF if require_explicit_off else _MONEY).search(text)
    if money:
        value = normalizar_dinheiro(money.group(1))
        if value > 0:
            return "fixo", value
    return "", 0.0


def _money_match(pattern, text):
    match = pattern.search(text or "")
    return normalizar_dinheiro(match.group(1)) if match else None


def _plain_html(value):
    return " ".join(
        html.unescape(_HTML_TAG.sub(" ", str(value or ""))).split(),
    )


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


def _walk_schema_offers(value):
    if isinstance(value, dict):
        if value.get("@type") == "Offer":
            yield value
        for child in value.values():
            yield from _walk_schema_offers(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_schema_offers(child)


def _next_flight_json_objects(body):
    """Extrai objetos JSON completos de payloads RSC sem cruzar cards."""
    prefix = "self.__next_f.push("
    decoder = json.JSONDecoder()
    for script in _SCRIPT.findall(body or ""):
        raw = script.strip()
        if not raw.startswith(prefix) or not raw.endswith(")"):
            continue
        try:
            payload = json.loads(raw[len(prefix):-1])
        except (TypeError, ValueError):
            continue
        if (
            not isinstance(payload, list) or len(payload) < 2
            or not isinstance(payload[1], str)
        ):
            continue
        text = payload[1]
        for marker in re.finditer(r"(?=\{)", text):
            try:
                value, _end = decoder.raw_decode(text, marker.start())
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                yield value


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
            benefit = str(row.get("discount") or "").strip()
            description = " ".join(filter(None, (
                benefit, str(row.get("name") or ""),
                str(row.get("description") or ""),
            )))
            # schema.org's dedicated ``discount`` field may legally be just
            # ``20%``.  Free text is different: a product title/price such as
            # ``Monitor 240 Hz — R$ 656`` is not a coupon benefit.  It must say
            # OFF/de desconto or the row fails closed.
            discount_type, discount = (
                _discount(benefit) if benefit
                else _discount(description, require_explicit_off=True)
            )
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


class DiscoupShopeeCouponsSource(_PublicCatalogSource):
    """Catalogo Shopee cujo popup publico revela codigo e regras completas."""

    slug = "discoup-cupons"
    name = "Discoup - catalogo publico de cupons Shopee"
    marketplace = "shopee"

    def __init__(self):
        super().__init__()
        self._popup_cache = {}

    def _catalog_rows(self, body):
        seen = set()
        for raw in _LD_JSON.findall(body or ""):
            try:
                block = json.loads(html.unescape(raw))
            except (TypeError, ValueError):
                continue
            for row in _walk_schema_offers(block):
                raw_id = str(row.get("@id") or "").rsplit("#", 1)[-1]
                if not _DISCOUP_ID.fullmatch(raw_id) or raw_id in seen:
                    continue
                seen.add(raw_id)
                yield raw_id, row

    def _download_popup(self, offer_id):
        now = time.monotonic()
        cached = self._popup_cache.get(offer_id)
        if cached and now - cached[0] < _DISCOUP_CACHE_SECONDS:
            return cached[1], True
        response = requests.get(
            _DISCOUP_POPUP.format(offer_id=offer_id), timeout=_TIMEOUT,
            headers={"User-Agent": _UA, "Referer": _DISCOUP_URL},
        )
        body = (
            response.content.decode("utf-8", errors="replace")
            if response.status_code == 200 else ""
        )
        if body:
            self._popup_cache[offer_id] = (now, body)
        return body, False

    def discover_coupons(self, marketplaces=None, **kwargs):
        selected = set(marketplaces or ("shopee",))
        if "shopee" not in selected:
            self.last_health_status = "healthy"
            self.last_metrics = {"lojas_lidas": 0, "cupons": 0, "complete": False}
            return
        observed_at = timezone.now()
        rejected = defaultdict(int)
        try:
            body = _download(_DISCOUP_URL)
        except requests.RequestException as exc:
            logger.info("%s unavailable (%s).", self.slug, type(exc).__name__)
            body = ""
        if not body:
            self.last_health_status = "degraded"
            self.last_metrics = {
                "lojas_lidas": 0, "popup_total": 0, "popup_falhas": 0,
                "cupons": 0, "complete": False,
            }
            return
        catalog = list(self._catalog_rows(body))

        def load(entry):
            offer_id, row = entry
            try:
                popup, cache_hit = self._download_popup(offer_id)
                return offer_id, row, popup, cache_hit
            except requests.RequestException:
                return offer_id, row, "", False

        with ThreadPoolExecutor(
            max_workers=min(_DISCOUP_WORKERS, max(1, len(catalog))),
        ) as executor:
            popups = list(executor.map(load, catalog))

        accepted = set()
        failures = cache_hits = 0
        for offer_id, row, popup, cache_hit in popups:
            cache_hits += int(cache_hit)
            if not popup:
                failures += 1
                rejected["popup_unavailable"] += 1
                continue
            decoded = html.unescape(popup)
            code_match = _DISCOUP_CODE.search(decoded)
            code = code_match.group(1).strip().upper() if code_match else ""
            description = " ".join(filter(None, (
                str(row.get("name") or ""), str(row.get("description") or ""),
            )))
            if re.search(r"\b(?:cashback|moedas?)\b", description, re.I):
                rejected["cashback"] += 1
                continue
            discount_type, discount = _discount(
                description, require_explicit_off=True,
            )
            valid_until = _when(row.get("validThrough"))
            if valid_until and valid_until < observed_at:
                rejected["expired"] += 1
                continue
            item = _item(
                source=self.slug, marketplace="shopee",
                external_id=f"discoup:shopee:{offer_id}", code=code,
                discount_type=discount_type, discount=discount,
                description=description, observed_at=observed_at,
                valid_from=_when(row.get("validFrom")), valid_until=valid_until,
                minimum=_money_match(_MINIMUM, description),
                maximum=_money_match(_MAXIMUM, description),
                restricted=tem_restricao_publico(description),
                evidence={
                    "transport": "discoup-schema-popup",
                    "public_offer_id": offer_id,
                },
            )
            if item is None:
                rejected["invalid_code_or_discount"] += 1
                continue
            if item.coupon_code in accepted:
                rejected["duplicate_code"] += 1
                continue
            accepted.add(item.coupon_code)
            yield item

        self.last_health_status = (
            "degraded" if failures > max(3, len(catalog) // 4) else "healthy"
        )
        self.last_metrics = {
            "lojas_lidas": 1, "rows_seen": len(catalog),
            "popup_total": len(catalog), "popup_falhas": failures,
            "popup_cache_hits": cache_hits, "cupons": len(accepted),
            "rejected_by_reason": dict(sorted(rejected.items())),
            "complete": False,
        }


class PromomiaShopeeCouponsSource(_PublicCatalogSource):
    """Cupons Shopee estruturados no RSC publico, nunca produtos promocionais."""

    slug = "promomia-cupons"
    name = "Promomia - catalogo publico de cupons Shopee"
    marketplace = "shopee"

    @staticmethod
    def _rows(body):
        for raw in _NEXT_FLIGHT_SCRIPT.findall(body or ""):
            chunk = raw.replace(r'\"', '"')
            coupon_id = re.search(r'"couponId":"([^"]+)"', chunk)
            store = re.search(r'"storeSlug":"([^"]+)"', chunk)
            code = re.search(r'"couponCode":"([A-Za-z0-9_-]{4,40})"', chunk)
            discount = re.search(r'"discountLabel":"([^"]+)"', chunk)
            expires = re.search(r'"children":"at[eé] (\d{2}/\d{2}/\d{4})"', chunk, re.I)
            title = re.search(r'"children":"(Cupom[^"]*)"', chunk, re.I)
            expired = re.search(r'"isExpired":(true|false)', chunk)
            if not all((coupon_id, store, code, discount, expires, title, expired)):
                continue
            yield {
                "id": coupon_id.group(1), "store": store.group(1),
                "code": code.group(1), "discount": discount.group(1),
                "expires": expires.group(1), "title": title.group(1),
                "expired": expired.group(1) == "true",
            }

    def discover_coupons(self, marketplaces=None, **kwargs):
        selected = set(marketplaces or ("shopee",))
        if "shopee" not in selected:
            self.last_health_status = "healthy"
            self.last_metrics = {"lojas_lidas": 0, "cupons": 0, "complete": False}
            return
        observed_at = timezone.now()
        rejected = defaultdict(int)
        try:
            body = _download(_PROMOMIA_URL)
        except requests.RequestException as exc:
            logger.info("%s unavailable (%s).", self.slug, type(exc).__name__)
            body = ""
        if not body:
            self.last_health_status = "degraded"
            self.last_metrics = {"lojas_lidas": 0, "cupons": 0, "complete": False}
            return
        rows = list(self._rows(body))
        accepted = set()
        for row in rows:
            if row["store"].casefold() != "shopee":
                rejected["wrong_marketplace"] += 1
                continue
            if row["expired"]:
                rejected["expired"] += 1
                continue
            description = f"{row['discount']} {row['title']}"
            if re.search(r"\b(?:cashback|moedas?)\b", description, re.I):
                rejected["cashback"] += 1
                continue
            discount_type, discount = _discount(row["discount"])
            try:
                day = datetime.strptime(row["expires"], "%d/%m/%Y").date()
                valid_until = timezone.make_aware(
                    datetime.combine(day, datetime_time.max),
                )
            except ValueError:
                rejected["invalid_expiry"] += 1
                continue
            if valid_until < observed_at:
                rejected["expired"] += 1
                continue
            item = _item(
                source=self.slug, marketplace="shopee",
                external_id=f"promomia:shopee:{row['id']}", code=row["code"],
                discount_type=discount_type, discount=discount,
                description=description, observed_at=observed_at,
                valid_until=valid_until,
                minimum=_money_match(_MINIMUM, description),
                maximum=_money_match(_MAXIMUM, description),
                restricted=tem_restricao_publico(description),
                evidence={"transport": "promomia-next-rsc"},
            )
            if item is None:
                rejected["invalid_code_or_discount"] += 1
                continue
            if item.coupon_code in accepted:
                rejected["duplicate_code"] += 1
                continue
            accepted.add(item.coupon_code)
            yield item
        self.last_health_status = "healthy"
        self.last_metrics = {
            "lojas_lidas": 1, "rows_seen": len(rows), "cupons": len(accepted),
            "rejected_by_reason": dict(sorted(rejected.items())),
            "complete": False,
        }


class CuponationShopeeCouponsSource(_PublicCatalogSource):
    """Cupons Shopee estruturados em objetos Voucher do RSC publico."""

    slug = "cuponation-cupons"
    name = "CupoNation - catalogo publico de cupons Shopee"
    marketplace = "shopee"

    @staticmethod
    def _rows(body):
        seen = set()
        required = {
            "idPool", "title", "voucherType", "endTime", "published", "code",
        }
        for row in _next_flight_json_objects(body):
            if not required.issubset(row):
                continue
            row_id = str(row.get("idPool") or "")
            if not row_id or row_id in seen:
                continue
            seen.add(row_id)
            yield row

    def discover_coupons(self, marketplaces=None, **kwargs):
        selected = set(marketplaces or ("shopee",))
        if "shopee" not in selected:
            self.last_health_status = "healthy"
            self.last_metrics = {"lojas_lidas": 0, "cupons": 0, "complete": False}
            return
        observed_at = timezone.now()
        rejected = defaultdict(int)
        try:
            body = _download(_CUPONATION_URL)
        except requests.RequestException as exc:
            logger.info("%s unavailable (%s).", self.slug, type(exc).__name__)
            body = ""
        if not body:
            self.last_health_status = "degraded"
            self.last_metrics = {"lojas_lidas": 0, "cupons": 0, "complete": False}
            return
        rows = list(self._rows(body))
        accepted = set()
        for row in rows:
            if not row.get("published"):
                rejected["unpublished"] += 1
                continue
            try:
                voucher_type = int(row.get("voucherType"))
            except (TypeError, ValueError):
                rejected["invalid_voucher_type"] += 1
                continue
            if voucher_type != 0:
                rejected["not_coupon"] += 1
                continue
            valid_until = _when(row.get("endTime"))
            if not valid_until or valid_until < observed_at:
                rejected["expired_or_missing_expiry"] += 1
                continue
            description = " ".join(filter(None, (
                str(row.get("title") or ""),
                str(row.get("caption1") or ""),
                str(row.get("caption2") or ""),
                str(row.get("termsAndConditions") or ""),
            )))
            if re.search(r"\b(?:cashback|gift\s*card|moedas?)\b", description, re.I):
                rejected["cashback_or_reward"] += 1
                continue
            discount_type, discount = _discount(
                description, require_explicit_off=True,
            )
            item = _item(
                source=self.slug, marketplace="shopee",
                external_id=f"cuponation:shopee:{row['idPool']}",
                code=row.get("code"), discount_type=discount_type,
                discount=discount, description=description,
                observed_at=observed_at,
                valid_from=_when(row.get("startTime")),
                valid_until=valid_until,
                minimum=_money_match(_MINIMUM, description),
                maximum=_money_match(_MAXIMUM, description),
                restricted=tem_restricao_publico(description),
                evidence={
                    "transport": "cuponation-next-rsc",
                    "verified_at": str(row.get("verified") or "")[:40],
                },
            )
            if item is None:
                rejected["invalid_code_or_discount"] += 1
                continue
            if item.coupon_code in accepted:
                rejected["duplicate_code"] += 1
                continue
            accepted.add(item.coupon_code)
            yield item
        self.last_health_status = "healthy"
        self.last_metrics = {
            "lojas_lidas": 1, "rows_seen": len(rows), "cupons": len(accepted),
            "rejected_by_reason": dict(sorted(rejected.items())),
            "complete": False,
        }


class CashbeShopeeCouponsSource(_PublicCatalogSource):
    """Cards públicos da Cashbe com código, regra numérica e expiração relativa."""

    slug = "cashbe-cupons"
    name = "Cashbe - catálogo público de cupons Shopee"
    marketplace = "shopee"

    @staticmethod
    def _rows(body):
        for index, card in enumerate(_CASHBE_CARD.findall(body or ""), start=1):
            title = _CASHBE_TITLE.search(card)
            code = _CASHBE_CODE.search(card)
            if not title or not code:
                continue
            yield {
                "id": index,
                "title": _plain_html(title.group(1)),
                "code": _plain_html(code.group(1)),
                "text": _plain_html(card),
            }

    @staticmethod
    def _discount(text):
        discount_type, discount = _discount(text, require_explicit_off=True)
        if discount:
            return discount_type, discount
        saving = _CASHBE_SAVING.search(str(text or ""))
        value = normalizar_dinheiro(saving.group(1)) if saving else 0.0
        return ("fixo", value) if value > 0 else ("", 0.0)

    @staticmethod
    def _valid_until(text, observed_at):
        folded = str(text or "").casefold()
        if "expirado" in folded or re.search(r"expira:\s*h[áa]", folded):
            return None, "expired"
        relative = re.search(
            r"expira:\s*em\s*(\d+)\s*(dias?|horas?|minutos?)", folded,
        )
        if relative:
            amount = int(relative.group(1))
            if amount <= 0:
                return None, "expired"
            unit = relative.group(2)
            if unit.startswith("dia"):
                delta = timedelta(days=amount)
            elif unit.startswith("hora"):
                delta = timedelta(hours=amount)
            else:
                delta = timedelta(minutes=amount)
            return observed_at + delta, ""
        return None, "missing_expiry"

    def discover_coupons(self, marketplaces=None, **kwargs):
        selected = set(marketplaces or ("shopee",))
        if "shopee" not in selected:
            self.last_health_status = "healthy"
            self.last_metrics = {"lojas_lidas": 0, "cupons": 0, "complete": False}
            return
        observed_at = timezone.now()
        rejected = defaultdict(int)
        try:
            body = _download(_CASHBE_URL)
        except requests.RequestException as exc:
            logger.info("%s unavailable (%s).", self.slug, type(exc).__name__)
            body = ""
        if not body:
            self.last_health_status = "degraded"
            self.last_metrics = {"lojas_lidas": 0, "cupons": 0, "complete": False}
            return
        rows = list(self._rows(body))
        accepted = set()
        for row in rows:
            valid_until, expiry_error = self._valid_until(row["text"], observed_at)
            if expiry_error:
                rejected[expiry_error] += 1
                continue
            discount_type, discount = self._discount(row["title"])
            item = _item(
                source=self.slug, marketplace="shopee",
                external_id=f"cashbe:shopee:{row['id']}:{row['code']}",
                code=row["code"], discount_type=discount_type,
                discount=discount, description=row["title"],
                observed_at=observed_at, valid_until=valid_until,
                minimum=_money_match(_MINIMUM, row["title"]),
                maximum=_money_match(_MAXIMUM, row["title"]),
                restricted=tem_restricao_publico(row["title"]),
                evidence={"transport": "cashbe-public-card"},
            )
            if item is None:
                rejected["invalid_code_or_discount"] += 1
                continue
            if item.coupon_code in accepted:
                rejected["duplicate_code"] += 1
                continue
            accepted.add(item.coupon_code)
            yield item
        self.last_health_status = "healthy"
        self.last_metrics = {
            "lojas_lidas": 1, "rows_seen": len(rows), "cupons": len(accepted),
            "rejected_by_reason": dict(sorted(rejected.items())),
            "complete": False,
        }


class PegueiBaratoAmazonCouponsSource(_PublicCatalogSource):
    """Radar Amazon cujo HTML público liga código, regra e destino da loja.

    O catálogo também repete ``COMPRANOAPP`` em cards que são apenas ofertas.
    Aceitamos esse código somente no card de primeira compra no app e rejeitamos
    qualquer benefício numérico que contradiga o único número do próprio código.
    O link de terceiro serve só para provar o host e nunca é persistido.
    """

    slug = "peguei-barato-cupons"
    name = "Peguei Barato - radar público de cupons Amazon"
    marketplace = "amazon"

    @staticmethod
    def _rows(body):
        markers = list(_PEGUEI_CARD_START.finditer(body or ""))
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
            chunk = body[marker.start():end]
            title = _PEGUEI_TITLE.search(chunk)
            code = _PEGUEI_CODE.search(chunk)
            destination = _PEGUEI_DESTINATION.search(chunk)
            if not all((title, code, destination)):
                yield None
                continue
            description = _PEGUEI_DESCRIPTION.search(chunk)
            yield {
                "id": marker.group(1).lower(),
                "title": _plain_html(title.group(1)),
                "description": _plain_html(description.group(1)) if description else "",
                "code": _plain_html(code.group(1)).upper(),
                "destination": html.unescape(destination.group(1)).strip(),
            }

    @staticmethod
    def _discount_hint(code):
        numbers = re.findall(r"\d+(?:[.,]\d+)?", str(code or ""))
        if len(numbers) != 1:
            return None
        try:
            value = float(numbers[0].replace(",", "."))
        except ValueError:
            return None
        return value if value >= 5 else None

    def discover_coupons(self, marketplaces=None, **kwargs):
        selected = set(marketplaces or ("amazon",))
        if "amazon" not in selected:
            self.last_health_status = "healthy"
            self.last_metrics = {"lojas_lidas": 0, "cupons": 0, "complete": False}
            return
        observed_at = timezone.now()
        rejected = defaultdict(int)
        try:
            body = _download(_PEGUEI_BARATO_URL)
        except requests.RequestException as exc:
            logger.info("%s unavailable (%s).", self.slug, type(exc).__name__)
            body = ""
        if not body:
            self.last_health_status = "degraded"
            self.last_metrics = {"lojas_lidas": 0, "cupons": 0, "complete": False}
            return

        rows = list(self._rows(body))
        accepted = set()
        for row in rows:
            if not row:
                rejected["malformed_card"] += 1
                continue
            if not _marketplace_host(row["destination"], "amazon"):
                rejected["wrong_marketplace_url"] += 1
                continue
            description = " ".join(filter(None, (row["title"], row["description"])))
            discount_type, discount = _discount(
                row["title"], require_explicit_off=True,
            )
            if not discount:
                rejected["missing_explicit_discount"] += 1
                continue
            code = row["code"]
            folded = description.casefold()
            if code == "COMPRANOAPP" and not (
                "primeira compra" in folded and "app" in folded
                and discount_type == "fixo" and discount == 20
            ):
                rejected["generic_code_on_offer"] += 1
                continue
            hint = self._discount_hint(code)
            if hint is not None and abs(hint - discount) > 0.001:
                rejected["code_discount_mismatch"] += 1
                continue
            item = _item(
                source=self.slug, marketplace="amazon",
                external_id=f"pegueibarato:amazon:{row['id']}", code=code,
                discount_type=discount_type, discount=discount,
                description=description, observed_at=observed_at,
                minimum=_money_match(_MINIMUM, description),
                maximum=_money_match(_MAXIMUM, description),
                restricted=tem_restricao_publico(description),
                evidence={
                    "transport": "peguei-barato-public-card",
                    "destination_host_verified": True,
                },
            )
            if item is None:
                rejected["invalid_code_or_discount"] += 1
                continue
            if item.coupon_code in accepted:
                rejected["duplicate_code"] += 1
                continue
            accepted.add(item.coupon_code)
            yield item
        self.last_health_status = "healthy" if rows else "degraded"
        self.last_metrics = {
            "lojas_lidas": int(bool(rows)), "rows_seen": len(rows),
            "cupons": len(accepted),
            "rejected_by_reason": dict(sorted(rejected.items())),
            "complete": False,
        }
