"""Estoque de links do dono: exporta URLs pendentes e importa os `meli.la` colados de volta."""
import re
from urllib.parse import urlparse

from django.utils import timezone

from apps.coupons.feed import BRT

from .keys import product_key
from .models import AffiliateLink, Offer

ALLOWED_AFFILIATE_HOSTS = ("meli.la", "mercadolivre.com.br", "mercadolibre.com")


def is_storefront(url: str) -> bool:
    """`/social/…` é a vitrine do afiliado, não o anúncio: o comprador cai na página dele e não
    no produto (regressão que o v1 já pagou). Nunca é link publicável."""
    return "/social/" in urlparse((url or "").strip()).path.lower()


def safe_affiliate_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    ok = parsed.scheme == "https" and any(
        host == h or host.endswith("." + h) for h in ALLOWED_AFFILIATE_HOSTS
    ) and not is_storefront(url)
    return url.strip() if ok else ""


def pending_urls(org, now=None) -> list[str]:
    """URLs de ofertas de hoje que ainda não têm link do dono (para colar no Link Builder)."""
    now = now or timezone.now()
    today = now.astimezone(BRT).date()
    have = set(AffiliateLink.objects.for_org(org).values_list("product_key", flat=True))
    seen, out = set(), []
    for offer in Offer.objects.filter(valid_on=today).order_by("-discount_pct", "id"):
        if offer.product_key not in have and offer.product_key not in seen:
            seen.add(offer.product_key)
            out.append(offer.url)
    return out


_PAIR = re.compile(r"(https?://\S+)\s+(https?://\S+)")


def import_pairs(org, text: str, etiqueta: str = "") -> dict:
    """Aceita linhas `url_do_produto  link_afiliado`. Devolve contagens (nada é adivinhado)."""
    stats = {"importados": 0, "rejeitados": 0}
    for line in text.splitlines():
        match = _PAIR.search(line)
        if not match:
            if line.strip():
                stats["rejeitados"] += 1
            continue
        source, affiliate = match.group(1), safe_affiliate_url(match.group(2))
        if not affiliate:
            stats["rejeitados"] += 1
            continue
        AffiliateLink.unscoped.update_or_create(
            org=org, product_key=product_key(source),
            defaults={"affiliate_url": affiliate, "etiqueta": etiqueta},
        )
        stats["importados"] += 1
    return stats
