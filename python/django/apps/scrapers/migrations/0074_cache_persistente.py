"""Tabela do cache que precisa sobreviver a restart.

O `cupom_extractor` guarda por 30 dias a leitura de cada mensagem de canal, porque
ler a mesma mensagem duas vezes é pagar duas vezes ao modelo. Em produção o cache
padrão é LocMem (não há Redis): morre a cada deploy e no desligamento noturno, e
não é compartilhado entre os processos do honcho — o cache de 30 dias valia, na
prática, até o próximo restart.

A tabela é criada pela role de migração e por isso herda as DEFAULT PRIVILEGES já
instaladas (arwd para `spreading_runtime` e `spreading_system`). Não é tabela de
tenant: não guarda dado de organização nenhuma, só o texto público do canal já
interpretado, então fica fora do RLS de propósito.
"""
from django.core.management import call_command
from django.db import migrations

TABELA = "spreading_cache"


def criar_tabela(apps, schema_editor):
    call_command(
        "createcachetable", TABELA,
        database=schema_editor.connection.alias, verbosity=0,
    )


def remover_tabela(apps, schema_editor):
    schema_editor.execute(f'DROP TABLE IF EXISTS "{TABELA}"')


class Migration(migrations.Migration):

    dependencies = [
        ("scrapers", "0073_deal_layer_nicho"),
    ]

    operations = [
        migrations.RunPython(criar_tabela, remover_tabela),
    ]
