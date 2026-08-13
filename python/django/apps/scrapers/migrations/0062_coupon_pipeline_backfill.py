from importlib import import_module

from django.db import migrations


def backfill_coupon_contracts(apps, schema_editor):
    migration = import_module(
        "apps.scrapers.migrations.0061_coupon_pipeline_contracts"
    )
    migration.backfill_coupon_contracts(apps, schema_editor)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("scrapers", "0061_coupon_pipeline_contracts"),
    ]

    operations = [
        # Keep the data work in its own atomic migration. PostgreSQL cannot
        # create the deferred indexes from 0061 while FK trigger events from
        # this backfill are pending in the same transaction.
        migrations.RunPython(backfill_coupon_contracts, noop_reverse),
    ]
