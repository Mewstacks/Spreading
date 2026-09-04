"""Chave natural de `Produto`: a mesma regra de canonicalização em todo writer.

Hoje cinco pontos de código gravam `Produto` (`sources/persistence.py`,
`scraper_mercadolivre/ofertas_scraper.py`, `scraper_amazon/ofertas_scraper.py`,
`awin.py`, `coupon_products.py`, `scraper_mercadolivre/scraper.py`) e só um
deles normaliza a URL antes de gravar. O mesmo anúncio do Mercado Livre chega
com `click1/mclics` de um lado e URL limpa de outro, e como não existe
constraint de unicidade em `Produto`, as duas viram linhas separadas.

Este módulo existe para ser a ÚNICA fonte da regra "qual é a chave deste
produto" — link canônico quando não há ASIN, ASIN quando há. Mudar a regra
depois de produção já ter dados exige uma migração de re-fusão; não é
decisão para tomar duas vezes em lugares diferentes do código.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

# Contrato: mudar esta regra invalida chaves já gravadas e exige migração de
# re-fusão (ver PLANO em C:\Users\gege\.claude\plans — fase "Produto duplicado").
VERSAO_CANONICA = 1


def link_canonico(marketplace: str, link: str) -> str:
    """URL estável para chave de upsert. String vazia = descarte o item.

    Contrato: idempotente — ``link_canonico(mkt, link_canonico(mkt, x)) ==
    link_canonico(mkt, x)``. Isso é o que permite rodar a normalização de novo
    sobre uma URL já normalizada sem mudar o resultado.
    """
    bruto = str(link or "").strip()
    if not bruto:
        return ""
    marketplace = str(marketplace or "").strip().lower()
    if marketplace == "mercadolivre":
        return _link_canonico_ml(bruto)
    if marketplace == "amazon":
        return _link_canonico_amazon(bruto)
    return _link_canonico_generico(bruto)


def _link_canonico_ml(bruto: str) -> str:
    # A regra do ML já existe e é testada: click1/mclics, matt_word/matt_tool,
    # MLB-id — mover para cá duplicaria lógica que o Link Builder mantém.
    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import (
        _normalizar_link_produto,
    )
    return _normalizar_link_produto(bruto)


def _link_canonico_amazon(bruto: str) -> str:
    """Amazon é chaveada por ASIN, não por link — isto é só o fallback textual
    para o raro caso de gravar sem ASIN resolvido (hoje os writers descartam
    esses itens; mantido para não deixar a função parcial por marketplace)."""
    return _link_canonico_generico(bruto)


def _link_canonico_generico(bruto: str) -> str:
    """scheme+host(lower)+path, sem query/fragmento, sem barra final."""
    try:
        p = urlsplit(bruto)
        host = (p.netloc or "").lower()
        path = (p.path or "").rstrip("/")
        limpo = urlunsplit((p.scheme, host, path, "", ""))
    except (TypeError, ValueError):
        limpo = bruto.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return limpo[:1000]


def chave_natural(*, marketplace: str, owner_id, asin: str = "", link: str = "") -> dict:
    """Dict de lookup pronto para `Produto.objects.update_or_create(**chave)`.

    Espelha a regra que `sources/persistence.py` já aplica manualmente: ASIN
    ganha da URL quando existe. `owner_id` pode ser um id ou um objeto
    (aceita ambos porque os writers hoje passam formas diferentes).
    """
    owner = getattr(owner_id, "pk", owner_id)
    chave = {"marketplace": marketplace, "owner": owner}
    asin = str(asin or "").strip()
    if asin:
        chave["asin"] = asin
    else:
        chave["link_produto"] = link_canonico(marketplace, link)
    return chave
