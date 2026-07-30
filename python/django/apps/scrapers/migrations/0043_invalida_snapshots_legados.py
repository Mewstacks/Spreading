from django.db import migrations


def invalidar_snapshots_legados(apps, schema_editor):
    ReceitaAfiliado = apps.get_model("scrapers", "ReceitaAfiliado")
    # Os relatórios antigos eram snapshots de janela móvel e não podem participar
    # da soma diária normalizada. Preservamos a auditoria, mas tiramos da projeção.
    ReceitaAfiliado.objects.filter(origem="auto", granularidade="etiqueta").update(
        origem="legacy")


class Migration(migrations.Migration):
    dependencies = [("scrapers", "0042_consolida_causas_de_incidente")]

    operations = [migrations.RunPython(invalidar_snapshots_legados, migrations.RunPython.noop)]
