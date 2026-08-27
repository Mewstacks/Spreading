from django.db import migrations, models


def criar_indice(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS scrape_price_stats_cover "
        "ON scrapers_precohistorico (marketplace, chave, data) INCLUDE (preco)"
    )


def remover_indice(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS scrape_price_stats_cover"
        )


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("scrapers", "0067_produto_upsert_lookup_index"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(criar_indice, remover_indice)],
            state_operations=[migrations.AddIndex(
                model_name="precohistorico",
                index=models.Index(
                    fields=["marketplace", "chave", "data"],
                    include=["preco"],
                    name="scrape_price_stats_cover",
                ),
            )],
        ),
    ]
