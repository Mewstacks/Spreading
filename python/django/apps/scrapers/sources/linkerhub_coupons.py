"""Conservative reader for LinkerHub's public multi-store coupon page.

The page is useful as community discovery evidence, but its cards are not a
source of truth: measured production HTML has contained both a card assigned to
the wrong marketplace and a displayed code contradicting its own description.
For that reason this adapter fails closed, never keeps the outbound affiliate
URL, and its source is classified as non-listable community evidence elsewhere.
"""
from __future__ import annotations

import html
import logging
import re
from collections import defaultdict
from datetime import datetime, time as datetime_time, timedelta
from html.parser import HTMLParser

import requests
from django.utils import timezone

from apps.scrapers.cupom_extractor import codigo_plausivel

from .base import SourceAdapter, normalizar_dinheiro
from .public_coupon_aggregators import _MAXIMUM, _MINIMUM, _item, _money_match


logger = logging.getLogger(__name__)
_URL = "https://www.linkerhub.com.br/cupons"
_TIMEOUT = (5, 20)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_MARKETPLACE_LABELS = {
    "amazon": "amazon",
    "mercado livre": "mercadolivre",
    "shopee": "shopee",
}
_MONEY_VALUE = r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
_PERCENT_OFF = re.compile(
    r"(?<![\d.,])(\d{1,2}(?:[.,]\d{1,2})?)\s*%\s*(?:off|de desconto)", re.I,
)
_FIXED_OFF = re.compile(
    rf"R\$\s*{_MONEY_VALUE}\s*(?:off|de desconto)", re.I,
)
_DATE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})(?!\d)")
_DAYS = re.compile(r"expira\s+em\s+(\d{1,3})\s+dias?", re.I)
_EXPLICIT_CODE = re.compile(
    r"(?:\bcupom\s*:?\s*|[🎟🎫]\ufe0f?\s*(?:cupom\s*:?\s*)?)"
    r"([A-Z0-9][A-Z0-9._-]{3,29})",
    re.I,
)
_PLACEHOLDERS = frozenset({
    "ANTES", "FUNCIONANDO", "RESGATE", "COPIAR", "PEGAR", "APLICAR",
})
_CROSS_MARKETPLACE_MARKERS = {
    "amazon": ("meli.la/", "mercadolivre.com", "shopee.com", "shope.ee/"),
    "mercadolivre": ("amazon.com", "amzn.to/", "shopee.com", "shope.ee/"),
    "shopee": ("amazon.com", "amzn.to/", "meli.la/", "mercadolivre.com"),
}


def _clean(value):
    # Some descriptions arrive double-escaped (``R&amp;#036;``).
    decoded = html.unescape(html.unescape(str(value or "")))
    return " ".join(decoded.replace("\xa0", " ").split())


class _CardParser(HTMLParser):
    """Extract semantic card fields without depending on third-party parsers."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards = []
        self._card = None
        self._div_depth = 0
        self._capture = None

    @staticmethod
    def _classes(attrs):
        return set(dict(attrs).get("class", "").split())

    def handle_starttag(self, tag, attrs):
        classes = self._classes(attrs)
        if tag == "div" and self._card is None and "glass-panel" in classes:
            self._card = defaultdict(list)
            self._div_depth = 1
            return
        if self._card is None:
            return
        if tag == "div":
            self._div_depth += 1

        field = ""
        if tag == "span" and "uppercase" in classes:
            field = "marketplace"
        elif tag == "span" and "text-zinc-500" in classes:
            field = "validity"
        elif tag == "h3":
            field = "discount"
        elif tag == "p" and "text-zinc-300" in classes:
            field = "description"
        elif tag == "div" and "font-mono" in classes:
            field = "code"
        if field and self._capture is None:
            self._capture = {"tag": tag, "field": field, "parts": []}

    def handle_data(self, data):
        if self._capture is not None:
            self._capture["parts"].append(data)

    def handle_endtag(self, tag):
        if self._card is None:
            return
        if self._capture is not None and tag == self._capture["tag"]:
            value = _clean("".join(self._capture["parts"]))
            if value:
                self._card[self._capture["field"]].append(value)
            self._capture = None
        if tag != "div":
            return
        self._div_depth -= 1
        if self._div_depth == 0:
            card = {
                field: values[0] if values else ""
                for field, values in self._card.items()
            }
            if card.get("marketplace") or card.get("code"):
                self.cards.append(card)
            self._card = None


def _parse_cards(body):
    parser = _CardParser()
    parser.feed(body or "")
    parser.close()
    return parser.cards


def _discount(text):
    text = _clean(text)
    percent = _PERCENT_OFF.search(text)
    if percent:
        value = float(percent.group(1).replace(",", "."))
        return ("porcentagem", value) if 0 < value < 100 else ("", 0.0)
    fixed = _FIXED_OFF.search(text)
    if fixed:
        value = normalizar_dinheiro(fixed.group(1))
        return ("fixo", value) if value > 0 else ("", 0.0)
    return "", 0.0


def _code_context(description, code):
    description = _clean(description)
    match = re.search(
        rf"(?<![A-Z0-9._-]){re.escape(code)}(?![A-Z0-9._-])",
        description, re.I,
    )
    if not match:
        return ""
    # Enough for ``R$ X OFF em R$ Y; limite R$ Z`` but deliberately short so a
    # later coupon in the shared list cannot lend this one its rules.
    return description[match.start():match.end() + 90]


def _discount_for_code(heading, description, code):
    """Tie a numeric benefit to this card instead of a neighbouring code.

    Some cards repeat a list such as ``CODE10: R$10 OFF; CODE70: R$70 OFF``.
    Reading the first amount for every displayed code would create internally
    consistent but false coupons.  A numeric heading applies to the card; an
    ``OFERTA`` heading requires the displayed code and its following amount.
    """
    direct = _discount(heading)
    if direct[0]:
        return direct
    context = _code_context(description, code)
    if not context:
        return "", 0.0
    # Coupon lists put the amount immediately after the code.  The small window
    # intentionally cannot drift into the next coupon in the same description.
    return _discount(context[:55])


def _end_of_day(day):
    return timezone.make_aware(
        datetime.combine(day, datetime_time.max), timezone.get_current_timezone(),
    )


def _valid_until(label, observed_at):
    text = _clean(label).casefold()
    if "expirou" in text:
        return None, True
    today = timezone.localtime(observed_at).date()
    explicit = _DATE.search(text)
    if explicit:
        year = int(explicit.group(3))
        if year < 100:
            year += 2000
        try:
            day = datetime(year, int(explicit.group(2)), int(explicit.group(1))).date()
        except ValueError:
            return None, True
        end = _end_of_day(day)
        return end, end < observed_at
    relative = _DAYS.search(text)
    if relative:
        return _end_of_day(today + timedelta(days=int(relative.group(1)))), False
    if "amanh" in text:
        return _end_of_day(today + timedelta(days=1)), False
    if "expira hoje" in text or "v\u00e1lido hoje" in text:
        return _end_of_day(today), False
    return None, False


def _explicit_codes(description):
    return {
        match.group(1).upper() for match in _EXPLICIT_CODE.finditer(description or "")
        if codigo_plausivel(match.group(1))
    }


class LinkerHubCouponsSource(SourceAdapter):
    slug = "linkerhub-cupons"
    name = "LinkerHub — catálogo público multiloja"
    marketplace = "multiloja"
    requires_chromium = False
    inventario_completo = False

    def __init__(self):
        self.last_metrics = {}
        self.last_health_status = "unknown"

    def _download(self):
        response = requests.get(_URL, timeout=_TIMEOUT, headers={"User-Agent": _UA})
        if response.status_code != 200:
            return ""
        return response.content.decode("utf-8", errors="replace")

    def discover_coupons(self, marketplaces=None, **kwargs):
        selected = set(marketplaces or _MARKETPLACE_LABELS.values())
        rejected = defaultdict(int)
        accepted = defaultdict(int)
        seen = set()
        try:
            body = self._download()
        except requests.RequestException as exc:
            logger.info("%s unavailable (%s).", self.slug, type(exc).__name__)
            body = ""
        if not body:
            self.last_health_status = "degraded"
            self.last_metrics = {
                "pages_read": 0, "rows_seen": 0, "cupons": 0,
                "accepted_by_marketplace": {}, "rejected_by_reason": {},
                "complete": False,
            }
            return

        observed_at = timezone.now()
        cards = _parse_cards(body)
        for card in cards:
            label = _clean(card.get("marketplace")).casefold()
            marketplace = _MARKETPLACE_LABELS.get(label, "")
            if not marketplace:
                rejected["unknown_marketplace"] += 1
                continue
            if marketplace not in selected:
                rejected["marketplace_not_requested"] += 1
                continue
            code = _clean(card.get("code")).upper()
            if code in _PLACEHOLDERS or not codigo_plausivel(code):
                rejected["invalid_or_placeholder_code"] += 1
                continue
            description = _clean(card.get("description"))
            lower_description = description.casefold()
            if any(
                marker in lower_description
                for marker in _CROSS_MARKETPLACE_MARKERS[marketplace]
            ):
                rejected["wrong_marketplace_url"] += 1
                continue
            described_codes = _explicit_codes(description)
            if (
                marketplace in {"amazon", "shopee"}
                and len(described_codes) == 1
                and code not in described_codes
            ):
                rejected["displayed_code_mismatch"] += 1
                continue
            discount_type, discount = _discount_for_code(
                card.get("discount", ""), description, code,
            )
            if not discount_type:
                rejected["no_explicit_coupon_discount"] += 1
                continue
            rule_context = _code_context(description, code) or description
            valid_until, expired = _valid_until(card.get("validity"), observed_at)
            if expired:
                rejected["expired"] += 1
                continue
            key = (marketplace, code)
            if key in seen:
                rejected["duplicate_code"] += 1
                continue
            item = _item(
                source=self.slug, marketplace=marketplace,
                external_id=f"linkerhub:{marketplace}:{code}", code=code,
                discount_type=discount_type, discount=discount,
                description=description, observed_at=observed_at,
                valid_until=valid_until,
                minimum=_money_match(_MINIMUM, rule_context),
                maximum=_money_match(_MAXIMUM, rule_context),
                evidence={"transport": "linkerhub-public-html"},
            )
            if item is None:
                rejected["invalid_code_or_discount"] += 1
                continue
            seen.add(key)
            accepted[marketplace] += 1
            yield item

        self.last_health_status = "healthy"
        self.last_metrics = {
            "pages_read": 1, "rows_seen": len(cards), "cupons": len(seen),
            "accepted_by_marketplace": dict(sorted(accepted.items())),
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
