import hashlib
import hmac

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_active_organization(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor == "postgresql" and settings.TENANT_CONTEXT_SIGNING_KEY:
        signature = hmac.new(
            settings.TENANT_CONTEXT_SIGNING_KEY.encode("utf-8"), b"system:",
            hashlib.sha256,
        ).hexdigest()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT set_config('app.system_context', 'on', true),
                       set_config('app.system_signature', %s, true)
                """,
                [signature],
            )
    Perfil = apps.get_model("accounts", "Perfil")
    Perfil.objects.filter(active_organization__isnull=True).update(
        active_organization=models.F("organization"),
    )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0019_mercadolivresession_linkbuilder_readiness")]

    operations = [
        migrations.AddField(
            model_name="perfil", name="active_organization",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="active_profiles", to="accounts.organization",
            ),
        ),
        migrations.AddField(model_name="whatsappconnection", name="capacity_max",
                            field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(model_name="whatsappconnection", name="capacity_used",
                            field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(
            model_name="whatsappconnection", name="consistency_status",
            field=models.CharField(db_index=True, default="unknown", max_length=24),
        ),
        migrations.AddField(model_name="whatsappconnection", name="last_event",
                            field=models.CharField(blank=True, default="", max_length=80)),
        migrations.AddField(
            model_name="whatsappconnection", name="last_event_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(model_name="whatsappconnection", name="masked_number",
                            field=models.CharField(blank=True, default="", max_length=24)),
        migrations.AddField(
            model_name="whatsappconnection", name="phase",
            field=models.CharField(blank=True, db_index=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="whatsappconnection", name="unavailable_reason",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
        migrations.AddField(
            model_name="whatsappconnection", name="worker_instance",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.CreateModel(
            name="OrganizationFeatureOverride",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("feature", models.CharField(max_length=80)),
                ("state", models.CharField(
                    choices=[("inherit", "Herdar"), ("enabled", "Habilitada"),
                             ("disabled", "Desabilitada")],
                    default="inherit", max_length=12,
                )),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="feature_overrides", to="accounts.organization",
                )),
            ],
            options={"constraints": [models.UniqueConstraint(
                fields=("organization", "feature"),
                name="uniq_organization_feature_override",
            )]},
        ),
        migrations.RunPython(backfill_active_organization, migrations.RunPython.noop),
    ]
