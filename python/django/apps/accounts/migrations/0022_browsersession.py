import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models

import apps.accounts.fields


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0021_perfil_pode_ligar_envio"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BrowserSession",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("provider", models.CharField(choices=[
                    ("amazon", "Amazon Relatórios"),
                    ("amazon_shop", "Amazon Compras"),
                    ("mercadolivre", "Mercado Livre Relatórios"),
                    ("shopee_shop", "Shopee Compras"),
                ], max_length=32)),
                ("encrypted_state", apps.accounts.fields.EncryptedTextField()),
                ("status", models.CharField(choices=[
                    ("active", "Ativa"), ("suspect", "Suspeita"),
                    ("decrypt_error", "Erro de criptografia"),
                ], db_index=True, default="active", max_length=24)),
                ("probe_failures", models.PositiveSmallIntegerField(default=0)),
                ("probe_result", models.CharField(blank=True, default="", max_length=24)),
                ("probe_reason", models.CharField(blank=True, default="", max_length=200)),
                ("last_probe_at", models.DateTimeField(blank=True, null=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("organization", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="browser_sessions", to="accounts.organization")),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="browser_sessions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [models.Index(
                    fields=["user", "provider", "status"],
                    name="accounts_br_user_id_4d89dd_idx")],
                "constraints": [models.UniqueConstraint(
                    fields=("organization", "user", "provider"),
                    name="uniq_browser_session_org_user_provider")],
            },
        ),
    ]
