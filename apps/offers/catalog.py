"""Coletor de catálogo do ML pela API oficial, guiado pelos termos das regras ativas."""
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from apps.coupons.feed import BRT

from . import ml_api
from .models import Offer, Rule

SOURCE = "ml-api-catalogo"


def split_terms(text: str) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def collect_catalog(now=None, per_term=15, http=None) -> int:
    now = now or timezone.now()
    today = now.astimezone(BRT).date()
    terms = set()
    for rule in Rule.unscoped.filter(active=True, type=Rule.OFFERS):
        terms.update(split_terms(rule.terms))
    count = 0
    for term in sorted(terms):
        for product in ml_api.search_products(term, per_term, http=http):
            price = ml_api.product_price(product["id"], http=http)
            if not price or not price["price"]:
                continue
            price_to = Decimal(str(price["price"]))
            price_from = Decimal(str(price["price_from"])) if price["price_from"] else None
            if not price_from or price_from <= price_to or not price["winner_tagged"]:
                continue  # sem preço de lista oficial ou ganhador marcado: não é provada
            pct = ((1 - price_to / price_from) * 100).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            Offer.objects.update_or_create(
                store="mercadolivre", product_key=product["id"], valid_on=today,
                defaults={
                    "source": SOURCE, "title": str(product.get("name") or product["id"])[:300],
                    "url": f"https://www.mercadolivre.com.br/p/{product['id']}",
                    "price_from": price_from, "price_to": price_to, "discount_pct": pct,
                    "vertical": "", "coupon_code": "", "last_seen": now,
                },
            )
            count += 1
    return count
