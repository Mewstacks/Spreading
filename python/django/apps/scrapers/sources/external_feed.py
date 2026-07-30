"""Ingestao do feed JSON entregue por uma rede de afiliados licenciada.

O conector aceita um envelope ``{"items": [...]}`` ou uma lista na raiz. Os nomes
canonicos dos campos sao ``type``, ``id``, ``marketplace``, ``title``, ``url``,
``coupon_code`` e ``valid_until``; aliases comuns em feeds de afiliacao tambem sao
aceitos para evitar que cada rede exija um adaptador quase identico.
"""

import hashlib
import re
from datetime import datetime, time
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.utils import timezone

from apps.scrapers.coupon_rules import normalizar_regras_cupom

from .base import IngestedItem, SourceAdapter


_MARKETPLACE_ALIASES = {
    "amazon": "amazon",
    "amazon brasil": "amazon",
    "amazon br": "amazon",
    "amazon.com.br": "amazon",
    "mercado livre": "mercadolivre",
    "mercadolivre": "mercadolivre",
    "mercado libre": "mercadolivre",
    "ml": "mercadolivre",
}
_COUPON_KINDS = {"coupon", "cupom", "voucher", "promo_code", "promocode"}


def _first(row, *keys, default=""):
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return default


def _marketplace(row):
    raw = _first(
        row, "marketplace", "store", "store_name", "merchant", "advertiser",
        "loja", "anunciante",
    )
    value = re.sub(r"[_-]+", " ", str(raw)).strip().lower()
    if value in _MARKETPLACE_ALIASES:
        return _MARKETPLACE_ALIASES[value]
    if "amazon" in value:
        return "amazon"
    if "mercado" in value and ("livre" in value or "libre" in value):
        return "mercadolivre"
    return ""


def _http_url(value):
    url = str(value or "").strip()
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _valid_until(value):
    """Converte datas ISO, brasileiras e epoch para datetime aware.

    Datas sem horario valem ate o fim do dia, evitando expirar um cupom logo no
    primeiro minuto da data final informada pela rede.
    """
    if value in (None, ""):
        return None
    parsed = None
    has_time = False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = datetime.fromtimestamp(value, tz=timezone.get_current_timezone())
            has_time = True
        except (ValueError, OSError, OverflowError):
            return None
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            has_time = "T" in text or " " in text
        except ValueError:
            for pattern in ("%d/%m/%Y", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(text, pattern)
                    break
                except ValueError:
                    continue
    if parsed is None:
        return None
    if not has_time:
        parsed = datetime.combine(parsed.date(), time(23, 59, 59))
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _is_coupon(row):
    kind = str(_first(row, "type", "kind", "item_type", "tipo")).strip().lower()
    return kind in _COUPON_KINDS or bool(
        _first(row, "coupon_code", "code", "coupon", "voucher_code", "codigo")
    )


class LicensedFeedSource(SourceAdapter):
    """Conector JSON generico; fica inativo enquanto a URL nao for configurada."""

    slug = "licensed-affiliate-feed"
    marketplace = "multiloja"
    name = "Feed licenciado de afiliados"

    def _rows(self):
        url = getattr(settings, "AFFILIATE_FEED_URL", "")
        if not url:
            return []
        headers = {"Accept": "application/json", "User-Agent": "Spreading/1.0 (+affiliate-feed)"}
        token = getattr(settings, "AFFILIATE_FEED_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            payload = payload.get("items", payload.get("coupons", payload.get("offers", [])))
        return payload if isinstance(payload, list) else []

    def discover_offers(self, **kwargs):
        for row in self._rows():
            if not isinstance(row, dict) or _is_coupon(row):
                continue
            url = _http_url(_first(row, "url", "affiliate_url", "deeplink", "tracking_url"))
            try:
                price = float(_first(row, "price", "current_price", "preco", default=0) or 0)
                before = float(_first(row, "before", "reference_price", "old_price", default=0) or 0)
            except (TypeError, ValueError):
                continue
            if not url or price <= 0 or before <= price:
                continue
            yield IngestedItem(
                external_id=str(row.get("id") or hashlib.sha256(url.encode()).hexdigest()),
                marketplace=_marketplace(row) or "multiloja", source=self.slug,
                kind="offer", canonical_url=url,
                title=str(_first(row, "title", "name", "nome"))[:255],
                current_price=price, reference_price=before, observed_at=timezone.now(),
                evidence={"transport": "licensed-json-feed"},
            )

    def discover_coupons(self, **kwargs):
        now = timezone.now()
        seen = set()
        for row in self._rows():
            if not isinstance(row, dict) or not _is_coupon(row):
                continue
            marketplace = _marketplace(row)
            if marketplace not in {"mercadolivre", "amazon"}:
                continue

            code = str(_first(
                row, "coupon_code", "code", "coupon", "voucher_code", "codigo",
            )).strip()
            # O feed precisa fornecer o deeplink da rede, nao apenas a pagina publica
            # da loja. Campos explicitamente afiliados tem precedencia sobre `url`.
            link = _http_url(_first(
                row, "affiliate_url", "deeplink", "tracking_url", "click_url", "url",
            ))
            if not code or not link:
                continue

            validity = _valid_until(_first(
                row, "valid_until", "expires_at", "end_date", "expiration_date",
                "validade", "data_fim",
            ))
            if validity and validity < now:
                continue

            title = str(_first(
                row, "title", "name", "description", "nome", "descricao",
                default=f"Cupom {code}",
            )).strip()[:255]
            source_id = _first(row, "id", "coupon_id", "external_id", "offer_id")
            if source_id:
                external_id = f"licensed:{marketplace}:{source_id}"
            else:
                identity = "|".join((
                    marketplace, code.upper(), validity.date().isoformat() if validity else "", title,
                ))
                external_id = "licensed:" + hashlib.sha256(identity.encode()).hexdigest()
            external_id = external_id[:160]
            if external_id in seen:
                continue
            seen.add(external_id)

            discount_value = _first(
                row, "discount_value", "discount", "value", "valor_desconto",
            )
            rules = normalizar_regras_cupom({
                "tipo_desconto": _first(
                    row, "discount_type", "type_discount", "tipo_desconto",
                ),
                "valor_desconto": discount_value,
                "discount_num": _first(row, "discount_percent", "percent_off"),
                "valor_minimo": _first(
                    row, "minimum_purchase", "min_purchase", "valor_minimo", "min_compra",
                ),
                "desconto_maximo": _first(
                    row, "maximum_discount", "max_discount", "desconto_maximo", "desconto_max",
                ),
                "escopo": _first(row, "scope", "category", "escopo", "categoria"),
                "modo_resgate": "codigo",
                "dia_inicio": _first(row, "start_date", "valid_from", "data_inicio"),
                "dia_fim": _first(
                    row, "end_date", "valid_until", "expires_at", "data_fim",
                ),
            }, external_id=external_id, codigo=code)

            yield IngestedItem(
                external_id=external_id,
                marketplace=marketplace,
                source=self.slug,
                kind="coupon",
                canonical_url=link[:1000],
                title=title,
                coupon_code=code[:120],
                coupon_rules=rules,
                valid_until=validity,
                observed_at=now,
                evidence={
                    "transport": "licensed-json-feed",
                    "network": str(_first(row, "network", "provider", "rede"))[:120],
                },
            )
