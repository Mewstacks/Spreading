"""Coluna de busca sem acento para Produto.

A busca da tela de Promoções usava `nome__icontains`, que no Postgres vira ILIKE
e é sensível a acento: quem digitava "robo" não encontrava nenhum item cujo título
traz "robô", e a tela parecia simplesmente não ter o produto.

`nome_norm` guarda o título em minúsculas e sem acento (ver
`apps.scrapers.models.normalizar_busca`). Daqui para a frente ela é mantida por
`Produto.save()` e pelo único `bulk_create` de Produto.

SÓ a coluna entra aqui — o preenchimento do catálogo que já existe é o comando
`backfill_nome_norm`, rodado DEPOIS do deploy.

Por quê: a primeira versão desta migração trazia o backfill junto, num laço que ia
até a fila esvaziar. Mas durante o release_command quem está servindo é o release
ANTERIOR, que não conhece esta coluna — então cada produto que a raspagem insere
naquele momento entra com `nome_norm` vazio. O laço drenava e o scraper repunha, e
o release ficou 8 minutos sem terminar até ser abortado (v164 failed, 2026-08-06).

Separar resolve nas duas pontas: o release passa a ser instantâneo, e o backfill
roda com o código novo já no ar, quando toda escrita nova já preenche a coluna
sozinha e a fila só diminui.
"""
from django.db import migrations, models


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
    ]
