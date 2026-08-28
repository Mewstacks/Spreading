from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scrapers", "0068_preco_historico_covering_index"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="linkafiliadousuario",
            index=models.Index(
                fields=["usuario", "verificado_ok", "-verificado_em"],
                name="linkusr_ready_recent_idx",
            ),
        ),
    ]
