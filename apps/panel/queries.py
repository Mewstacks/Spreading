"""Consultas compartilhadas pelas telas do painel."""
from datetime import timedelta

from django.db.models import Q

from apps.offers.models import AffiliateLink, Offer

FRESH = timedelta(days=14)


def with_link(org):
    """Q das ofertas que já têm o link do dono (Amazon: basta a tag da organização; par de cupom
    por código do ML usa o link do produto)."""
    keys = AffiliateLink.objects.for_org(org).values("product_key")
    has = Q(link_key__in=keys) | Q(store="mercadolivre", coupon_kind="code", product_key__in=keys)
    return has | Q(store="amazon") if org.amazon_tag else has


def live_offers(now):
    return Offer.objects.filter(last_seen__gte=now - FRESH).exclude(image_url="")


def live_coupons(now):
    return live_offers(now).exclude(coupon_kind="").exclude(coupon_until__lt=now)
