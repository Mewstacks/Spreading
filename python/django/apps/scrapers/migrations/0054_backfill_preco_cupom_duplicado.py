"""Desfaz o desconto de cupom aplicado duas vezes no catálogo do Mercado Livre.

`_sincronizar_produtos_no_banco` (scraper_mercadolivre/scraper.py) gravava em
`preco_com_cupom`/`preco_efetivo` o preço JÁ descontado pelo cupom, enquanto
`coupon_products.calcular_precos` desconta o cupom por conta própria — as duas
contas se somavam e a mensagem anunciava um valor que a loja não cobrava.

A re-raspagem não conserta sozinha: `scraper.main` monta `ja_feitos` e pula toda
campanha que já tenha produto, então as linhas erradas ficam congeladas.

Discriminador: o produtor correto (`coupon_products._coletar_ml_remoto`) grava
`preco_com_cupom == preco_fonte`; o legado grava `preco_com_cupom < preco_fonte`
E `preco_sem_desconto == preco_fonte` (a vitrine ia no campo do preço de lista).

`preco_sem_desconto` fica igual à vitrine — o preço de lista foi descartado na
gravação e não dá para reconstruí-lo. Isso é tratado: `calcular_precos` normaliza
`original < atual` e `montar_mensagem` esconde o "DE" quando não há desconto real,
então a mensagem passa a mostrar só o "POR". A recoleta natural repõe o "DE".
"""
import logging

from django.db import migrations
from django.db.models import F

logger = logging.getLogger(__name__)


def desfazer_desconto_duplicado(apps, schema_editor):
    Produto = apps.get_model("scrapers", "Produto")
    afetados = Produto.objects.filter(
        marketplace="mercadolivre",
        owner__isnull=True,
        origem="cupom",
        fonte="mercadolivre-cupom",
        preco_fonte__isnull=False,
        preco_com_cupom__lt=F("preco_fonte"),
        preco_sem_desconto=F("preco_fonte"),
    ).update(
        preco_com_cupom=F("preco_fonte"),
        preco_efetivo=F("preco_fonte"),
    )
    # O número importa na verificação do deploy: zero significa que o diagnóstico
    # da dupla aplicação precisa ser reaberto antes de confiar no resto.
    logger.info("Backfill de preço duplicado do ML: %s produto(s) corrigido(s)", afetados)


class Migration(migrations.Migration):

    dependencies = [
        ("scrapers", "0053_execucao_raspagem_resiliente"),
    ]

    operations = [
        migrations.RunPython(
            desfazer_desconto_duplicado,
            migrations.RunPython.noop,
            elidable=False,
        ),
    ]
