from django.db import migrations, models


def marcar_historico_processado(apps, schema_editor):
    # A 0037 já reconcilia todo o histórico ao criar a projeção de incidentes.
    apps.get_model("scrapers", "EventoOperacional").objects.update(incidente_processado=True)


class Migration(migrations.Migration):
    dependencies = [("scrapers", "0037_incidentes_saude")]

    operations = [
        migrations.AddField(
            model_name="eventooperacional",
            name="incidente_processado",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.RunPython(marcar_historico_processado, migrations.RunPython.noop),
    ]
