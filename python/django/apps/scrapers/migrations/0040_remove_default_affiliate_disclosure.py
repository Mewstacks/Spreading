from django.db import migrations


DISCLOSURE = "Este conteúdo contém link de afiliado."


def remove_default_disclosure(apps, schema_editor):
    ConfiguracaoEnvio = apps.get_model("scrapers", "ConfiguracaoEnvio")
    ConfiguracaoEnvio.objects.filter(divulgacao_afiliado=DISCLOSURE).update(
        divulgacao_afiliado=""
    )


class Migration(migrations.Migration):
    dependencies = [("scrapers", "0039_publicacao_slug_curto")]

    operations = [
        migrations.RunPython(remove_default_disclosure, migrations.RunPython.noop),
    ]
