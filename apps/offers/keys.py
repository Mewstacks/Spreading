"""Identidade de produto (ideia do v1: chave natural estável a partir da URL)."""
import re
import unicodedata
from urllib.parse import urlparse

from apps.compose.offer import short_name

_ITEM = re.compile(r"(MLB)-?(\d{6,})", re.I)
_CATALOG = re.compile(r"/p/(MLB\d+)", re.I)


def product_key(url: str) -> str:
    """`/p/MLB1` -> `MLB1` (catálogo); `MLB-123456` -> `MLB123456` (anúncio); senão host+path."""
    parsed = urlparse((url or "").strip())
    catalog = _CATALOG.search(parsed.path)
    if catalog:
        return catalog.group(1).upper()
    item = _ITEM.search(parsed.path)
    if item:
        return f"{item.group(1).upper()}{item.group(2)}"
    return f"{(parsed.hostname or '').lower()}{parsed.path.rstrip('/')}"[:300]


def slug_identity(text: str) -> str:
    """Minúsculas, sem acento nem pontuação, uma palavra por espaço."""
    folded = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()[:140]


def title_identity(title: str) -> str:
    """Identidade de modelo: o nome curto que o grupo vê na mensagem. Anúncios diferentes com o
    mesmo nome (a Shopee tem vários vendedores do mesmo produto) contam como um só."""
    return slug_identity(short_name(title))
