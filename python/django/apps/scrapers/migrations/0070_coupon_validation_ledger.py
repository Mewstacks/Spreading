from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def install_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    from apps.accounts.rls import policy_statements

    with schema_editor.connection.cursor() as cursor:
        for statement in policy_statements(
            "scrapers_cupomvalidacao", mixed=False,
            system_role=settings.TENANT_SYSTEM_DB_ROLE,
            migration_role=settings.TENANT_MIGRATION_DB_ROLE,
        ):
            cursor.execute(statement)


def uninstall_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            'ALTER TABLE "scrapers_cupomvalidacao" NO FORCE ROW LEVEL SECURITY'
        )
        cursor.execute(
            'ALTER TABLE "scrapers_cupomvalidacao" DISABLE ROW LEVEL SECURITY'
        )


class Migration(migrations.Migration):
    dependencies = [("scrapers", "0069_link_usuario_ready_recent_index")]

    operations = [
        migrations.CreateModel(
            name="CupomValidacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("marketplace", models.CharField(db_index=True, max_length=20)),
                ("product_key", models.CharField(blank=True, db_index=True,
                                                  default="", max_length=160)),
                ("product_url", models.URLField(blank=True, default="", max_length=1500)),
                ("cart_fingerprint", models.CharField(db_index=True, max_length=64)),
                ("status", models.CharField(
                    choices=[("pending", "Pendente"), ("running", "Em execução"),
                             ("accepted", "Aceito"), ("rejected", "Rejeitado"),
                             ("inconclusive", "Inconclusivo")],
                    db_index=True, default="pending", max_length=20,
                )),
                ("reason_code", models.CharField(blank=True, db_index=True,
                                                 default="", max_length=64)),
                ("safe_detail", models.CharField(blank=True, default="", max_length=255)),
                ("subtotal_before", models.DecimalField(blank=True, decimal_places=2,
                                                        max_digits=12, null=True)),
                ("subtotal_after", models.DecimalField(blank=True, decimal_places=2,
                                                       max_digits=12, null=True)),
                ("discount_amount", models.DecimalField(blank=True, decimal_places=2,
                                                        max_digits=12, null=True)),
                ("evidence", models.JSONField(blank=True, default=dict)),
                ("no_purchase", models.BooleanField(default=True)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("verified_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("retry_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("cupom", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="validacoes", to="scrapers.cupomnormalizado",
                )),
                ("organization", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="validacoes_cupons", to="accounts.organization",
                )),
                ("usuario", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="validacoes_cupons", to=settings.AUTH_USER_MODEL,
                )),
            ],
        ),
        migrations.AddConstraint(
            model_name="cupomvalidacao",
            constraint=models.UniqueConstraint(
                fields=("usuario", "cupom", "cart_fingerprint"),
                name="uniq_coupon_cart_validation",
            ),
        ),
        migrations.AddConstraint(
            model_name="cupomvalidacao",
            constraint=models.CheckConstraint(
                condition=models.Q(("no_purchase", True)),
                name="coupon_validation_never_purchases",
            ),
        ),
        migrations.AddIndex(
            model_name="cupomvalidacao",
            index=models.Index(fields=["usuario", "status", "verified_at"],
                               name="coupon_validation_user_status"),
        ),
        migrations.AddIndex(
            model_name="cupomvalidacao",
            index=models.Index(fields=["marketplace", "verified_at"],
                               name="coupon_validation_market_time"),
        ),
        migrations.RunPython(install_rls, uninstall_rls),
    ]
