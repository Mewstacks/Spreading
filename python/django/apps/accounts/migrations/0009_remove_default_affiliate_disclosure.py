from django.db import migrations, models


DISCLOSURE = "Este conteúdo contém link de afiliado."


def remove_default_disclosure(apps, schema_editor):
    Perfil = apps.get_model("accounts", "Perfil")
    Perfil.objects.filter(divulgacao_afiliado=DISCLOSURE).update(divulgacao_afiliado="")


class Migration(migrations.Migration):
    dependencies = [("accounts", "0008_alter_perfil_telegram_bot_token")]

    operations = [
        migrations.RunPython(remove_default_disclosure, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="perfil",
            name="divulgacao_afiliado",
            field=models.CharField(blank=True, default="", max_length=180),
        ),
    ]
