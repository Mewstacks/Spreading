"""Expira as associações cupom–produto que nunca foram provadas.

Duas origens, medidas em produção em 20/08/2026, quando 45 das 73 publicações de
24 horas saíram com o mesmo código:

1. **Alegação de site inteiro desmentida pela própria fonte.** A página oficial de
   afiliados publicou o MELIPROMO duas vezes: uma linha com ``is_mar_aberto: true``
   e outra com escopo ``Vehicle Parts & Accessories``. `persist_items` nunca apaga,
   então as duas ficaram ativas. `coupon_products._site_inteiro` acreditou na
   primeira e gravou 32 vínculos "confirmados" — parafusadeira, whey, monitor,
   panelas — e a mensagem passou a anunciar o código em qualquer oferta. O
   checkout do ML dizia a verdade: "este cupom ainda pode ser usado em produtos
   selecionados".

2. **Vitrine genérica lida como lista do cupom.** `_coletar_ml_remoto` aceitava
   qualquer URL ``*.mercadolivre.com.br`` como a listagem do cupom, inclusive
   ``/ofertas/cupons`` e a home. Ambas devolvem dezenas de cards, e cada card
   virava um vínculo confirmado.

Aqui os vínculos viram ``expirado`` — o mesmo estado que `preparar_cupom` usa
quando um produto deixa de ser aplicável. Nada é apagado: se a associação for
legítima, a próxima preparação a reconfirma. As preparações afetadas são
reagendadas para que isso aconteça no primeiro ciclo depois do deploy, em vez de
esperar a validade do cupom.

Associação de container (``regra="container"``) e listagens reais não são tocadas.
"""
from urllib.parse import urlsplit

from django.db import migrations

HOST_LISTAGEM = "lista.mercadolivre.com.br"


def _e_listagem(url) -> bool:
    try:
        partes = urlsplit(str(url or ""))
    except ValueError:
        return False
    if partes.scheme not in ("http", "https"):
        return False
    return (partes.hostname or "").casefold().rstrip(".") == HOST_LISTAGEM


def _codigos_contestados(CupomNormalizado):
    """Códigos que existem como site inteiro E como recorte, ambos ativos."""
    site, estreito = set(), set()
    for cupom in CupomNormalizado.objects.filter(estado="ativo").only(
            "id", "codigo", "regras"):
        codigo = str(cupom.codigo or "").strip().upper()
        if not codigo:
            continue
        regras = cupom.regras or {}
        aberto = bool(regras.get("is_mar_aberto") or regras.get("site_wide") is True)
        (site if aberto else estreito).add(codigo)
    return site & estreito


def expirar(apps, schema_editor):
    CupomNormalizado = apps.get_model("scrapers", "CupomNormalizado")
    ProdutoCupom = apps.get_model("scrapers", "ProdutoCupom")
    CupomPreparacao = apps.get_model("scrapers", "CupomPreparacao")

    contestados = _codigos_contestados(CupomNormalizado)
    cupons_contestados = set()
    if contestados:
        for cupom in CupomNormalizado.objects.filter(estado="ativo").only(
                "id", "codigo", "regras"):
            regras = cupom.regras or {}
            if not (regras.get("is_mar_aberto") or regras.get("site_wide") is True):
                continue
            if str(cupom.codigo or "").strip().upper() in contestados:
                cupons_contestados.add(cupom.id)

    condenadas = set(
        ProdutoCupom.objects.filter(
            status="confirmado", cupom_id__in=cupons_contestados,
        ).values_list("id", flat=True)
    ) if cupons_contestados else set()

    for relacao in ProdutoCupom.objects.filter(
            status="confirmado", evidencia__regra="pagina_oficial",
    ).only("id", "evidencia").iterator(chunk_size=2000):
        if not _e_listagem((relacao.evidencia or {}).get("url")):
            condenadas.add(relacao.id)

    if not condenadas:
        return
    cupons = set(
        ProdutoCupom.objects.filter(id__in=condenadas)
        .values_list("cupom_id", flat=True)
    )
    ProdutoCupom.objects.filter(id__in=condenadas).update(status="expirado")
    # `verificado_em=None` tira o preparo da janela de cache de 3h; sem isso o
    # cupom continuaria "pronto" apoiado nos vínculos que acabaram de cair.
    CupomPreparacao.objects.filter(cupom_id__in=cupons).update(
        status="vazio", verificado_em=None, proxima_tentativa=None,
        reason_code="scope_undelimited", produtos_chave="",
    )


class Migration(migrations.Migration):

    dependencies = [("scrapers", "0063_coupon_pipeline_constraints")]

    operations = [
        migrations.RunPython(expirar, migrations.RunPython.noop, elidable=True),
    ]
