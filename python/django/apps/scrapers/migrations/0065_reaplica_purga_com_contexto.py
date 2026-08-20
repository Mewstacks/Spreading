"""Reaplica a 0064 nos bancos onde ela rodou sem contexto de sistema.

A 0064 original não abria `app.system_context`. Em SQLite isso é indiferente e a
suíte passou; em produção o RLS escondeu todas as linhas da conexão da migração,
o `update()` percorreu um conjunto vazio e a migração foi registrada como aplicada
sem ter expirado um único vínculo — os produtos seguiram "confirmados" para o
cupom desmentido.

A 0064 foi corrigida para bancos novos. Esta migração existe para os que já a
registraram: chama a mesma função, agora com o contexto aberto. Onde a 0064 já fez
o trabalho, aqui não sobra nada.
"""
from importlib import import_module

from django.db import migrations

_ORIGEM = import_module(
    "apps.scrapers.migrations.0064_purga_associacao_vitrine_generica")


def expirar(apps, schema_editor):
    _ORIGEM.expirar(apps, schema_editor)


class Migration(migrations.Migration):

    dependencies = [("scrapers", "0064_purga_associacao_vitrine_generica")]

    operations = [
        migrations.RunPython(expirar, migrations.RunPython.noop, elidable=True),
    ]
