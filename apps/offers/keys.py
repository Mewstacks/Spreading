"""Identidade de produto (ideia do v1: chave natural estável a partir da URL)."""
import re
from urllib.parse import urlparse

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
