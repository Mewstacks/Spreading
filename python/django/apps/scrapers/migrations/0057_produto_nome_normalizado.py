"""Coluna de busca sem acento para Produto, mais o backfill do catálogo atual.

A busca da tela de Promoções usava `nome__icontains`, que no Postgres vira ILIKE
e é sensível a acento: quem digitava "robo" não encontrava nenhum item cujo título
traz "robô", e a tela parecia simplesmente não ter o produto.

`nome_norm` guarda o título em minúsculas e sem acento (ver
`apps.scrapers.models.normalizar_busca`). Daqui para a frente ela é mantida por
`Produto.save()` e pelo único `bulk_create` de Produto; esta migração preenche o
que já está no banco, em lotes, para não montar um UPDATE único sobre o catálogo
inteiro durante o release.
"""
import logging
import unicodedata

from django.db import migrations, models

logger = logging.getLogger(__name__)

TAMANHO_LOTE = 2000


def _normalizar(texto) -> str:
    # Cópia deliberada de models.normalizar_busca: migração não importa código de
    # aplicação, que muda com o tempo e mudaria o resultado desta migração
    # retroativamente.
    if not texto:
        return ""
    decomposto = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in decomposto if not unicodedata.combining(c)).casefold()


def preencher(apps, schema_editor):
    Produto = apps.get_model("scrapers", "Produto")
    total = 0
    while True:
        lote = list(
            Produto.objects.filter(nome_norm="").exclude(nome="")
            .only("id", "nome")[:TAMANHO_LOTE]
        )
        if not lote:
            break
        for produto in lote:
            produto.nome_norm = _normalizar(produto.nome)[:300]
        Produto.objects.bulk_update(lote, ["nome_norm"])
        total += len(lote)
        # Um item cujo nome normaliza para "" (título só de pontuação) voltaria no
        # próximo lote para sempre. Não existe hoje, mas o loop não pode depender
        # disso — sem esta saída o release travaria.
        if all(not p.nome_norm for p in lote):
            logger.warning("Backfill parou: %s item(ns) sem nome normalizável.",
                           len(lote))
            break
    logger.info("Backfill de nome_norm: %s produto(s).", total)


class Migration(migrations.Migration):

    dependencies = [
        ('scrapers', '0056_termo_busca_sem_teto'),
    ]

    operations = [
        migrations.AddField(
            model_name='produto',
            name='nome_norm',
            field=models.CharField(blank=True, default='', max_length=300),
        ),
        migrations.RunPython(preencher, migrations.RunPython.noop, elidable=False),
    ]
