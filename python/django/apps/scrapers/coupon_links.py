from datetime import timedelta

from django.conf import settings
from django.utils import timezone


def coupon_link_verified_and_fresh(link, *, now=None) -> bool:
    """True somente para cache de cupom aprovado e dentro do TTL operacional."""
    if link is None or link.verificado_ok is not True:
        return False
    if not (link.url_canonica or link.link_afiliado):
        return False
    # Caches históricos de produto já tinham veredito, mas a coluna
    # ``verificado_em`` só passou a ser obrigatória depois. ``criado_em`` (ou o
    # timestamp de atualização do cache de cupom) é o limite conservador durante
    # a janela de migração; links novos sempre gravam ``verificado_em``.
    verified_at = (
        link.verificado_em
        or getattr(link, "atualizado_em", None)
        or getattr(link, "criado_em", None)
    )
    if verified_at is None:
        return False
    ttl_hours = int(getattr(settings, "COUPON_AFFILIATE_LINK_TTL_HOURS", 168) or 168)
    return verified_at >= (now or timezone.now()) - timedelta(hours=max(1, ttl_hours))


def canonical_coupon_link(link) -> str:
    return str(getattr(link, "url_canonica", "") or getattr(link, "link_afiliado", "") or "")
