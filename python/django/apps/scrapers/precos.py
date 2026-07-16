"""Histórico de preços — registrar observações e medir se a oferta é REAL.

O "de/por" do marketplace é pouco confiável (preço "de" inflado). Comparando o
preço atual com o próprio histórico do item detectamos queda genuína (perto da
mínima) e descartamos "desconto" que na verdade é o preço de sempre.
"""
from datetime import timedelta

from django.utils import timezone

from apps.scrapers.models import PrecoHistorico


def chave_de(marketplace: str, asin: str = "", link: str = "") -> str:
    """Identidade estável do produto p/ o histórico. asin > URL normalizada."""
    mkt = marketplace or "mercadolivre"
    if asin:
        return f"{mkt}:asin:{asin}"
    base = (link or "").split("?")[0].split("#")[0].rstrip("/")
    return f"{mkt}:url:{base}"[:300]


def chave_produto(produto) -> str:
    return chave_de(
        getattr(produto, "marketplace", "mercadolivre"),
        getattr(produto, "asin", "") or "",
        getattr(produto, "link_produto", "") or "",
    )


def registrar(marketplace: str, asin: str, link: str, preco: float) -> None:
    """Grava uma observação de preço (silencioso em erro — nunca derruba a raspagem)."""
    if not preco or preco <= 0:
        return
    try:
        PrecoHistorico.objects.create(
            marketplace=marketplace or "mercadolivre",
            chave=chave_de(marketplace, asin, link), preco=float(preco),
        )
    except Exception:
        pass


def registrar_varios(items) -> None:
    """Bulk a partir de objetos/dicts com marketplace/asin/link_produto/preco_com_cupom."""
    linhas = []
    for it in items:
        get = (it.get if isinstance(it, dict) else lambda k, d=None: getattr(it, k, d))
        preco = get("preco_com_cupom", 0) or 0
        if preco <= 0:
            continue
        mkt = get("marketplace", "mercadolivre") or "mercadolivre"
        linhas.append(PrecoHistorico(
            marketplace=mkt,
            chave=chave_de(mkt, get("asin", "") or "", get("link_produto", "") or ""),
            preco=float(preco),
        ))
    if linhas:
        try:
            PrecoHistorico.objects.bulk_create(linhas, batch_size=500)
        except Exception:
            pass


def stats_em_lote(produtos, dias: int = 30) -> dict:
    """{chave: {n, minimo}} dos últimos `dias`, para uma lista de produtos — UMA query.

    A listagem chamava stats() por item: uma query por produto da página, cada uma
    trazendo TODAS as observações pra ordenar em Python. Aqui o banco agrega, e só o
    que a listagem usa (n e mínimo — a mediana ninguém lia).

    Filtra por marketplace junto com a chave pra bater com o índice composto
    (marketplace, chave, data); a chave sozinha já é única, mas não é prefixo dele.
    """
    from django.db.models import Count, Min

    if not produtos:
        return {}
    desde = timezone.now() - timedelta(days=dias)
    linhas = (
        PrecoHistorico.objects.filter(
            marketplace__in={getattr(p, "marketplace", "mercadolivre") for p in produtos},
            chave__in={chave_produto(p) for p in produtos},
            data__gte=desde,
        )
        .values("chave").annotate(n=Count("id"), minimo=Min("preco"))
    )
    return {l["chave"]: {"n": l["n"], "minimo": l["minimo"]} for l in linhas}


def stats(produto, dias: int = 30):
    """{n, minimo, mediana} das observações dos últimos `dias`. None se sem histórico."""
    desde = timezone.now() - timedelta(days=dias)
    precos = list(
        PrecoHistorico.objects.filter(
            marketplace=getattr(produto, "marketplace", "mercadolivre"),
            chave=chave_produto(produto), data__gte=desde,
        ).values_list("preco", flat=True)
    )
    if not precos:
        return None
    precos.sort()
    n = len(precos)
    mediana = precos[n // 2] if n % 2 else (precos[n // 2 - 1] + precos[n // 2]) / 2
    return {"n": n, "minimo": precos[0], "mediana": mediana}
