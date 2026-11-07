"""URL que entra no Link Builder: a página do produto, limpa (porta de `_montar_url_isca` do v1).

O Programa só aceita página de anúncio. Perfil, vitrine (`/social/`), catálogo `/up/` e links de
rastreio (`click1`, `/mclics/`) voltam vazios: sem isca válida não há o que gerar.
"""
import re
from urllib.parse import parse_qs, urlparse

FORBIDDEN_PATHS = ("/social/", "/perfil/", "/usuario/", "/noindex/", "/up/", "/mclics/")
CAMPAIGN = re.compile(r"^\d{3,12}$")


def _clean(url: str) -> str:
    try:
        parsed = urlparse((url or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    if not (host == "mercadolivre.com.br" or host.endswith(".mercadolivre.com.br")):
        return ""
    if host.startswith("click1.") or parsed.username or parsed.password:
        return ""
    if port not in (None, 80, 443):
        return ""
    if any(part in parsed.path.lower() for part in FORBIDDEN_PATHS):
        return ""
    return f"https://{host}{parsed.path}"


def bait_url(offer) -> str:
    """Produto sem query nem fragmento; com `coupon_campaign_id` quando o par traz uma campanha.

    Cupom de ativação sem campanha legível não ativa nada no link, então não gera isca.
    """
    if offer.store != "mercadolivre":
        return ""
    base = _clean(offer.url)
    if not base:
        return ""
    query = parse_qs(urlparse(offer.source_url or "").query)
    campaign = (query.get("coupon_campaign_id") or [""])[0]
    if CAMPAIGN.match(campaign):
        return f"{base}?coupon_campaign_id={campaign}"
    return "" if offer.coupon_kind == "activation" else base
