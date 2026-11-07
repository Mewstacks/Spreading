"""Feed `ofertas-diarias.html` da equipe de afiliados do ML (preço de/por oficial do dia)."""
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.coupons.feed import BRT, extract_array, fetch_feed, parse_br_number

from . import niches
from .keys import product_key
from .models import Offer

SOURCE = "ml-ofertas-diarias"
# Política do ML (itens vedados): título com estes termos nunca vira oferta.
PROHIBITED_TERMS = (
    "usado", "seminovo", "recondicionado", "medicamento", "remedio", "remédio",
    "carro", "moto ", "imovel", "imóvel", "apartamento", "terreno",
)


def daily_offers_url() -> str:
    base = settings.ML_COUPON_FEED_URL.rstrip("/")
    return f"{base}/ofertas-diarias.html"


def is_prohibited(title: str) -> bool:
    low = f" {title.lower()} "
    return any(term in low for term in PROHIBITED_TERMS)


def _discount(price_from, price_to, raw) -> Decimal:
    if price_from and price_to and price_from > price_to:
        pct = (Decimal(1) - price_to / price_from) * 100
        return pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    value = parse_br_number(raw)
    return value if value is not None and price_from and price_to != price_from else Decimal(0)


def parse_daily_offers(html: str, today):
    out = []
    for row in extract_array(html, "ITEMS"):
        title = str(row.get("produto") or "").strip()
        url = str(row.get("url") or "").strip()
        host = (urlparse(url).hostname or "").lower()
        try:
            valid_on = datetime.strptime(str(row.get("data_oferta")), "%d/%m/%Y").date()
        except ValueError:
            continue
        official_host = host.endswith(".mercadolivre.com.br") or host == "mercadolivre.com.br"
        if not title or valid_on != today or urlparse(url).scheme != "https" or not official_host:
            continue
        price_to = parse_br_number(row.get("preco_por"))
        price_from = parse_br_number(row.get("preco_de"))
        if not price_to or price_to <= 0 or is_prohibited(title):
            continue
        out.append({
            "title": title[:300], "url": url, "price_from": price_from, "price_to": price_to,
            "discount_pct": _discount(price_from, price_to, row.get("desconto")),
            "vertical": str(row.get("vertical") or "")[:80], "valid_on": valid_on,
        })
    return out


@transaction.atomic
def ingest_daily_offers(html: str, now: datetime | None = None) -> int:
    now = now or timezone.now()
    today = now.astimezone(BRT).date()
    count = 0
    for item in parse_daily_offers(html, today):
        key = product_key(item["url"])
        Offer.objects.update_or_create(
            store="mercadolivre", link_key=key, valid_on=item["valid_on"],
            defaults={
                "source": SOURCE, "product_key": key, "source_url": item["url"],
                "title": item["title"], "url": item["url"],
                "macro": niches.classify(item["title"]),
                "price_from": item["price_from"], "price_to": item["price_to"],
                "discount_pct": item["discount_pct"], "vertical": item["vertical"],
                "last_seen": now,
            },
        )
        count += 1
    return count


def fetch_daily_offers(session=None) -> str:
    return fetch_feed(daily_offers_url(), session=session)
