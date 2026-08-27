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
    """{chave: {n, minimo, mediana}} dos últimos `dias` em UMA query.

    A listagem e o ranking chamavam ``stats()`` por item. No ranking produtivo isso
    chegava a 400 consultas por regra de envio. Carregamos preço/chave uma vez,
    ordenamos no banco e calculamos a mesma mediana de ``stats`` em memória.

    Filtra por marketplace junto com a chave pra bater com o índice composto
    (marketplace, chave, data); a chave sozinha já é única, mas não é prefixo dele.
    """
    from functools import reduce
    from operator import or_

    from django.db.models import Q

    if not produtos:
        return {}
    desde = timezone.now() - timedelta(days=dias)
    pares = sorted({
        (getattr(p, "marketplace", "mercadolivre"), chave_produto(p))
        for p in produtos
    })
    from django.db import connection

    if connection.vendor == "postgresql":
        # O caminho ORM portátil devolve cada observação para o Python. Em produção
        # há mais de um milhão delas; para centenas de chaves isso ainda transferia
        # e ordenava dezenas de milhares de linhas. O PostgreSQL calcula os três
        # agregados junto ao índice e devolve uma linha por produto.
        tabela = connection.ops.quote_name(PrecoHistorico._meta.db_table)
        valores = ", ".join(["(%s, %s)"] * len(pares))
        parametros = [valor for par in pares for valor in par]
        parametros.append(desde)
        sql = f"""
            WITH requested(marketplace, chave) AS (VALUES {valores})
            SELECT h.chave,
                   COUNT(*)::bigint,
                   MIN(h.preco),
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY h.preco)
              FROM {tabela} h
              JOIN requested r
                ON r.marketplace = h.marketplace AND r.chave = h.chave
             WHERE h.data >= %s
             GROUP BY h.marketplace, h.chave
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, parametros)
            return {
                chave: {
                    "n": int(n), "minimo": float(minimo),
                    "mediana": float(mediana),
                }
                for chave, n, minimo, mediana in cursor.fetchall()
            }

    pares_q = reduce(or_, (Q(marketplace=marketplace, chave=chave)
                           for marketplace, chave in pares))
    linhas = (
        PrecoHistorico.objects.filter(pares_q, data__gte=desde)
        .order_by("chave", "preco")
        .values_list("chave", "preco")
    )
    precos_por_chave = {}
    for chave, preco in linhas:
        precos_por_chave.setdefault(chave, []).append(preco)
    resultado = {}
    for chave, precos in precos_por_chave.items():
        n = len(precos)
        mediana = (
            precos[n // 2]
            if n % 2
            else (precos[n // 2 - 1] + precos[n // 2]) / 2
        )
        resultado[chave] = {
            "n": n, "minimo": precos[0], "mediana": mediana,
        }
    return resultado


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
