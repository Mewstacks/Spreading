"""Identidade estável de produto para ranking e exibição.

As URLs do Mercado Livre carregam parâmetros de campanha e de variação. O mesmo
anúncio pode, por isso, ocupar várias linhas no catálogo mesmo tendo o mesmo nome.
Não apagamos essas observações (links e histórico podem apontar para elas), mas só
uma deve disputar ranking ou aparecer para o usuário.
"""

import re
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit


def _texto_normalizado(valor: str) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = texto.encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", texto))


def _id_mercado_livre(url: str) -> str:
    url = unquote(str(url or ""))
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)

    # `wid`/`item_id` identificam o anúncio específico e têm precedência sobre o
    # id de catálogo do path /p/MLB....
    for chave in ("wid", "item_id"):
        for valor in query.get(chave, []):
            match = re.search(r"MLB\d+", valor, re.I)
            if match:
                return match.group(0).upper()
    filtros = " ".join(query.get("pdp_filters", []))
    match = re.search(r"item_id\s*:\s*(MLB\d+)", filtros, re.I)
    if match:
        return match.group(1).upper()

    match = re.search(r"/p/(MLB\d+)", parsed.path, re.I)
    if match:
        return match.group(1).upper()
    match = re.search(r"/MLB-?(\d{6,})", parsed.path, re.I)
    if match:
        return f"MLB{match.group(1)}"
    return ""


def identidade_produto(produto) -> str:
    """Chave conservadora: marketplace + id estável + título normalizado.

    O título faz parte da chave para não colapsar variações explicitamente
    nomeadas (cor/tamanho). Variações com o mesmo título são duplicatas de UX e a
    observação mais recente é a fonte mais segura para preço.
    """
    marketplace = str(getattr(produto, "marketplace", "") or "").lower()
    titulo = _texto_normalizado(getattr(produto, "nome", ""))
    if marketplace == "amazon":
        asin = str(getattr(produto, "asin", "") or "").upper()
        if asin:
            return f"amazon:{asin}:{titulo}"
    if marketplace == "mercadolivre":
        item_id = _id_mercado_livre(getattr(produto, "link_produto", ""))
        if item_id:
            return f"mercadolivre:{item_id}:{titulo}"

    url = str(getattr(produto, "link_produto", "") or "")
    if not url:
        return f"pk:{getattr(produto, 'pk', id(produto))}"
    parsed = urlsplit(url)
    canonica = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                           parsed.path.rstrip("/"), "", ""))
    return f"{marketplace}:url:{canonica}:{titulo}"


def deduplicar_por_produto(itens, produto_de=lambda item: item):
    """Mantém a primeira observação de cada identidade na ordem recebida."""
    resultado = []
    vistos = set()
    for item in itens:
        chave = identidade_produto(produto_de(item))
        if chave in vistos:
            continue
        vistos.add(chave)
        resultado.append(item)
    return resultado
