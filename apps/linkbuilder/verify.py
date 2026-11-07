"""Para onde o link gerado leva de verdade (o que o v1 chama de verificação de destino).

Um GET anônimo, sem cookie da conta, que só segue o redirecionamento do `meli.la` e lê a URL
final. Regra central herdada do v1: bloqueio nunca condena o link. Verificação, login, 403/429/5xx
ou corpo de challenge viram "inconclusivo" (tenta de novo depois); só a vitrine do afiliado, uma
página que não é produto ou um anúncio morto recusam.
"""
import re

import requests

from .pw import is_interstitial, is_login

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}
ITEM_PATH = re.compile(r"/MLB-?\d{6,}")


def is_product_page(url: str) -> bool:
    return "produto.mercadolivre" in url or "/p/MLB" in url or bool(ITEM_PATH.search(url))


def verify_landing(link: str, http=None, timeout: float = 10) -> tuple[str, str]:
    """('ok', url_final) | ('recusado', motivo) | ('inconclusivo', motivo)."""
    try:
        response = (http or requests).get(
            link, allow_redirects=True, timeout=timeout, headers=HEADERS
        )
    except requests.exceptions.RequestException as exc:
        return "inconclusivo", f"rede: {type(exc).__name__}"
    final = response.url or ""
    if is_interstitial(final) or is_login(final):
        return "inconclusivo", "o ML pediu verificação ou login"
    code = response.status_code
    if code in (401, 403, 429) or code >= 500:
        return "inconclusivo", f"http {code}"
    if code in (404, 410):
        return "recusado", "anuncio_indisponivel"
    if "/social/" in final.lower():
        return "recusado", "destino_vitrine"
    if not is_product_page(final):
        return "recusado", f"destino_inesperado: {final[:80]}"
    body = response.text or ""
    if code == 200 and len(body) < 20000 and "andes-" not in body.lower():
        return "inconclusivo", "a resposta não parece uma página do Mercado Livre"
    return "ok", final
