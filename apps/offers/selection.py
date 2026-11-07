"""Quando uma regra pode publicar e qual oferta ela escolhe."""
import unicodedata
from datetime import timedelta

from django.conf import settings

from apps.coupons.feed import BRT
from apps.delivery.models import OfferDelivery

from .catalog import split_terms
from .models import AffiliateLink, Offer


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in text if unicodedata.category(c) != "Mn").lower()


def in_window(rule, now) -> bool:
    local = now.astimezone(BRT)
    days = {int(d) for d in rule.weekdays.split(",") if d.strip().isdigit()}
    if days and local.isoweekday() not in days:
        return False
    start, end, hour = rule.window_start, rule.window_end, local.hour
    if start == end:
        return True  # janela de 24 h
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # cruza a meia-noite


def matches_terms(rule, title: str) -> bool:
    low = _fold(title)
    include = [_fold(t) for t in split_terms(rule.terms)]
    exclude = [_fold(t) for t in split_terms(rule.negative_terms)]
    if exclude and any(t in low for t in exclude):
        return False
    return not include or any(t in low for t in include)


def pick_offer(org, destination, rule, now):
    """Melhor oferta publicável: do dia, fresca, com desconto, com link do dono, sem cooldown."""
    today = now.astimezone(BRT).date()
    fresh = now - timedelta(seconds=settings.FEED_MAX_AGE_SECONDS)
    links = dict(
        AffiliateLink.objects.for_org(org).values_list("product_key", "affiliate_url")
    )
    cooldown_since = now - timedelta(hours=rule.cooldown_hours)
    recent = set(
        OfferDelivery.objects.for_org(org)
        .filter(destination=destination, created_at__gte=cooldown_since)
        .values_list("product_key", flat=True)
    )
    candidates = Offer.objects.filter(
        valid_on=today, last_seen__gte=fresh, discount_pct__gte=rule.min_discount_pct
    ).order_by("-discount_pct", "price_to", "id")
    for offer in candidates:
        if offer.product_key in recent or offer.product_key not in links:
            continue
        if rule.price_min is not None and offer.price_to < rule.price_min:
            continue
        if rule.price_max is not None and offer.price_to > rule.price_max:
            continue
        if not matches_terms(rule, offer.title):
            continue
        return offer, links[offer.product_key]
    return None
