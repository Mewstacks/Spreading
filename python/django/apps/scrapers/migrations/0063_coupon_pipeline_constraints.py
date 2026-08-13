from importlib import import_module

from django.db import migrations, models


CONSTRAINT_NAME = "aviso_cupom_ativo_exige_marketplace"


def _constraint():
    return models.CheckConstraint(
        condition=(~models.Q(tipo="aviso_cupons")
                   | ~models.Q(marketplace="") | models.Q(ativo=False)),
        name=CONSTRAINT_NAME,
    )


class AddConstraintIfMissing(migrations.AddConstraint):
    """Tolerate an environment that applied the original, unsplit 0061."""

    def _exists(self, schema_editor, model):
        with schema_editor.connection.cursor() as cursor:
            constraints = schema_editor.connection.introspection.get_constraints(
                cursor, model._meta.db_table,
            )
        return self.constraint.name in constraints

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        if not self._exists(schema_editor, model):
            super().database_forwards(
                app_label, schema_editor, from_state, to_state,
            )

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        model = from_state.apps.get_model(app_label, self.model_name)
        if self._exists(schema_editor, model):
            super().database_backwards(
                app_label, schema_editor, from_state, to_state,
            )


def install_relation_link_rls(apps, schema_editor):
    migration = import_module(
        "apps.scrapers.migrations.0061_coupon_pipeline_contracts"
    )
    migration.install_relation_link_rls(apps, schema_editor)


def uninstall_relation_link_rls(apps, schema_editor):
    migration = import_module(
        "apps.scrapers.migrations.0061_coupon_pipeline_contracts"
    )
    migration.uninstall_relation_link_rls(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("scrapers", "0062_coupon_pipeline_backfill"),
    ]

    operations = [
        migrations.RunPython(install_relation_link_rls, uninstall_relation_link_rls),
        AddConstraintIfMissing(
            model_name="configuracaoenvio",
            constraint=_constraint(),
        ),
    ]
