from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scrapers", "0066_automacao_estado_compartilhado"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="produto",
            index=models.Index(
                fields=["marketplace", "owner", "link_produto"],
                name="scrapers_prod_lookup_idx",
            ),
        ),
    ]
