from django.db import migrations


def remove_legacy_chave(apps, schema_editor):
    """Remove a column left by pre-migration versions of the application.

    `HistoricoEnvio` has been keyed by `produto_id` since migration 0006, but
    some production databases predate that migration history and still carry a
    required `chave` column.  Django does not include that unknown column in an
    INSERT, so every successful delivery otherwise ends with a NOT NULL error.
    """
    table = apps.get_model("scrapers", "HistoricoEnvio")._meta.db_table
    connection = schema_editor.connection
    quote = connection.ops.quote_name

    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(cursor, table)
        }
        if "chave" not in columns:
            return

        # Drop any index referencing the legacy column first: SQLite rebuilds
        # the table on DROP COLUMN and chokes if a stale index still points at it.
        stale_indexes = [
            name
            for name, info in connection.introspection.get_constraints(
                cursor, table
            ).items()
            if info.get("index") and "chave" in info.get("columns", [])
        ]

    for name in stale_indexes:
        schema_editor.execute(f"DROP INDEX IF EXISTS {quote(name)}")

    schema_editor.execute(
        f"ALTER TABLE {quote(table)} DROP COLUMN {quote('chave')}"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("scrapers", "0029_eventooperacional"),
    ]

    operations = [
        migrations.RunPython(remove_legacy_chave, migrations.RunPython.noop),
    ]
