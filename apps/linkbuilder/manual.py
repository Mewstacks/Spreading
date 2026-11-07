"""Plano B sem automação: o painel lista as URLs que faltam, o dono gera os links no Link Builder
da própria conta (no navegador dele) e cola `url link` de volta."""
import re

from apps.offers.links import safe_affiliate_url
from apps.offers.models import AffiliateLink

from .bait import bait_url
from .runner import ETIQUETA_MANUAL, wanted

URL = re.compile(r"https?://\S+")


def pending(org, now, limit: int = 60) -> list[tuple[str, str]]:
    """[(url para o Link Builder, link_key)] do que as regras ativas publicariam, pares primeiro."""
    out, seen = [], set()
    for offer in wanted(org, now, limit * 3, respect_waits=False):
        bait = bait_url(offer)
        if bait and bait not in seen:
            seen.add(bait)
            out.append((bait, offer.link_key))
        if len(out) >= limit:
            break
    return out


def import_pasted(org, text: str, now) -> dict:
    """Linhas `url_enviada link_gerado`. Só aceita a URL que o painel pediu e link `meli.la`."""
    index = dict(pending(org, now, 1000))
    stats = {"importados": 0, "rejeitados": 0}
    for line in text.splitlines():
        urls = URL.findall(line)
        if not line.strip():
            continue
        key = index.get(urls[0].rstrip(".,;")) if len(urls) >= 2 else None
        link = safe_affiliate_url(urls[1].rstrip(".,;")) if len(urls) >= 2 else ""
        if not key or not link:
            stats["rejeitados"] += 1
            continue
        AffiliateLink.unscoped.update_or_create(
            org=org, product_key=key, defaults={"affiliate_url": link, "etiqueta": ETIQUETA_MANUAL})
        stats["importados"] += 1
    return stats
