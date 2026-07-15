from django.db import migrations


def remove_legacy_produto_chave(apps, schema_editor):
    """Remove coluna órfã que impede INSERTs no catálogo em bancos antigos."""
    table = apps.get_model("scrapers", "Produto")._meta.db_table
    connection = schema_editor.connection
    quote = connection.ops.quote_name

    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(cursor, table)
        }
        if "chave" not in columns:
            return
        stale_constraints = [
            (name, info)
            for name, info in connection.introspection.get_constraints(cursor, table).items()
            if "chave" in info.get("columns", [])
        ]

    for name, info in stale_constraints:
        if info.get("primary_key") or info.get("unique"):
            schema_editor.execute(
                f"ALTER TABLE {quote(table)} DROP CONSTRAINT IF EXISTS {quote(name)}"
            )
        elif info.get("index"):
            schema_editor.execute(f"DROP INDEX IF EXISTS {quote(name)}")

    schema_editor.execute(
        f"ALTER TABLE {quote(table)} DROP COLUMN {quote('chave')}"
    )


class Migration(migrations.Migration):
    dependencies = [("scrapers", "0031_ingestion_sources_and_coupon_catalog")]

    operations = [
        migrations.RunPython(remove_legacy_produto_chave, migrations.RunPython.noop),
    ]
