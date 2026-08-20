"""Apaga as associações cupom–produto colhidas numa vitrine genérica do ML.

`coupon_products._coletar_ml_remoto` aceitava qualquer URL `*.mercadolivre.com.br`
como "a lista de produtos deste cupom". Dois endereços caíam aí sem ser lista de
nada: `https://www.mercadolivre.com.br/ofertas/cupons`, gravado em todo código
raspado da vitrine, e `https://www.mercadolivre.com.br/`, usado como destino de
reserva quando a fonte oficial não publica um container. As duas páginas respondem
200 com dezenas de cards, e cada card virava um `ProdutoCupom` "confirmado".

O efeito em produção: um cupom de 25% restrito a `Vehicle Parts & Accessories`
apareceu anunciado num tablet e num jogo de panelas, porque esses itens estavam na
vitrine no momento da coleta. O código foi corrigido, mas as relações já gravadas
continuariam sendo publicadas até o cupom expirar — por isso a limpeza.

Critério: apaga somente `regra="pagina_oficial"` cuja `url` NÃO é uma listagem
(`lista.mercadolivre.com.br`). Associação de container (`regra="container"`) e as
listagens legítimas ficam intactas. O preparo de cada cupom afetado é reagendado
para que o pipeline reavalie o escopo com o código novo.
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


def purgar(apps, schema_editor):
    ProdutoCupom = apps.get_model("scrapers", "ProdutoCupom")
    CupomPreparacao = apps.get_model("scrapers", "CupomPreparacao")

    suspeitas = ProdutoCupom.objects.filter(evidencia__regra="pagina_oficial")
    condenadas = [
        relacao.id for relacao in suspeitas.only("id", "evidencia").iterator()
        if not _e_listagem((relacao.evidencia or {}).get("url"))
    ]
    if not condenadas:
        return
    cupons = set(
        ProdutoCupom.objects.filter(id__in=condenadas)
        .values_list("cupom_id", flat=True)
    )
    ProdutoCupom.objects.filter(id__in=condenadas).delete()
    # `verificado_em=None` tira o preparo da janela de cache de 3h; sem isso o
    # cupom continuaria "pronto" com as relações que acabaram de ser apagadas.
    CupomPreparacao.objects.filter(cupom_id__in=cupons).update(
        status="vazio", verificado_em=None, proxima_tentativa=None,
        reason_code="scope_undelimited", produtos_chave="",
    )


class Migration(migrations.Migration):

    dependencies = [("scrapers", "0063_coupon_pipeline_constraints")]

    operations = [
        migrations.RunPython(purgar, migrations.RunPython.noop, elidable=True),
    ]
