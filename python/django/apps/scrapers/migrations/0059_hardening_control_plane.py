import hashlib
import hmac

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


def backfill_publication_state(apps, schema_editor):
    """Alinha publicações históricas sem reabrir entregas já finalizadas."""
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
    Publicacao = apps.get_model("scrapers", "Publicacao")
    stages = {
        "pendente": "reserved",
        "enviado": "confirmed",
        "falhou": "permanent_failed",
        "incerto": "uncertain",
        "ignorado": "rejected",
    }
    for publication in Publicacao.objects.all().iterator(chunk_size=500):
        raw = (
            f"{publication.organization_id}:{publication.canal}:"
            f"{publication.destino_id}:{publication.id_publico}"
        )
        publication.operation_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        publication.stage = stages.get(publication.status, "reserved")
        publication.save(update_fields=["operation_key", "stage"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0020_perfil_active_organization_and_more"),
        ("scrapers", "0058_regra_dias_semana_tipo_e_pausa"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkerHeartbeat",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("worker_id", models.CharField(max_length=120, unique=True)),
                ("worker_type", models.CharField(db_index=True, max_length=40)),
                ("state", models.CharField(db_index=True, default="idle", max_length=24)),
                ("task_type", models.CharField(blank=True, default="", max_length=40)),
                ("heartbeat_at", models.DateTimeField(
                    db_index=True, default=django.utils.timezone.now)),
                ("details", models.JSONField(blank=True, default=dict)),
            ],
        ),
        migrations.AddField(model_name="execucaoingestao", name="duracoes",
                            field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(
            model_name="execucaoingestao", name="health_status",
            field=models.CharField(blank=True, db_index=True, default="unknown", max_length=24),
        ),
        migrations.AddField(model_name="execucaoingestao", name="metricas",
                            field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(model_name="execucaoingestao", name="paginas_processadas",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="execucaoingestao", name="rejeicoes",
                            field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(model_name="execucaoingestao", name="schema_fingerprint",
                            field=models.CharField(blank=True, default="", max_length=64)),
        migrations.AddField(model_name="execucaoraspagem", name="deadline_em",
                            field=models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField(model_name="execucaoraspagem", name="espera_iniciada_em",
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="execucaoraspagem", name="eta_amostra",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="execucaoraspagem", name="eta_max_em",
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="execucaoraspagem", name="eta_min_em",
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="execucaoraspagem", name="lease_token",
                            field=models.CharField(blank=True, default="", max_length=64)),
        migrations.AddField(model_name="execucaoraspagem", name="lock_owner_tipo",
                            field=models.CharField(blank=True, default="", max_length=40)),
        migrations.AddField(
            model_name="execucaoraspagem", name="motivo_espera",
            field=models.CharField(blank=True, db_index=True, default="", max_length=40),
        ),
        migrations.AddField(model_name="execucaoraspagem", name="posicao_fila",
                            field=models.PositiveIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="execucaoraspagem", name="recurso_esperado",
                            field=models.CharField(blank=True, default="", max_length=160)),
        migrations.AddField(
            model_name="execucaoraspagem", name="worker_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=120),
        ),
        migrations.AddField(model_name="publicacao", name="attempt_count",
                            field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(model_name="publicacao", name="duration_ms",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="publicacao", name="next_retry_at",
                            field=models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField(
            model_name="publicacao", name="operation_key",
            field=models.CharField(blank=True, max_length=160, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="publicacao", name="stage",
            field=models.CharField(db_index=True, default="selected", max_length=32),
        ),
        migrations.AddField(model_name="publicacao", name="transport_state",
                            field=models.CharField(blank=True, default="", max_length=32)),
        migrations.AddField(model_name="relatoriosync", name="linhas_aceitas",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="relatoriosync", name="linhas_rejeitadas",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="relatoriosync", name="linhas_vistas",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="relatoriosync", name="periodo_aplicado_fim",
                            field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="relatoriosync", name="periodo_aplicado_inicio",
                            field=models.DateField(blank=True, null=True)),
        migrations.AddField(
            model_name="relatoriosync", name="prerequisite_code",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        migrations.AddField(model_name="relatoriosync", name="schema_fingerprint",
                            field=models.CharField(blank=True, default="", max_length=64)),
        migrations.CreateModel(
            name="CupomDisponibilidade",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("channel", models.CharField(default="whatsapp", max_length=20)),
                ("use_mode", models.CharField(default="product_activation", max_length=24)),
                ("stage", models.CharField(choices=[
                    ("collected", "Coletado"), ("eligible", "Elegível"),
                    ("prepared", "Preparado"), ("waiting_link", "Aguardando link"),
                    ("ready", "Pronto"), ("discarded", "Descartado")],
                    db_index=True, default="collected", max_length=24)),
                ("category", models.CharField(blank=True, choices=[
                    ("", "Sem bloqueio"), ("not_found", "Não encontrado"),
                    ("rejected", "Rejeitado"), ("waiting", "Aguardando"),
                    ("no_session", "Sem sessão"), ("no_link", "Sem link"),
                    ("invalid", "Inválido"),
                    ("operational_failure", "Falha operacional")],
                    db_index=True, default="", max_length=32)),
                ("reason_code", models.CharField(blank=True, db_index=True,
                                                 default="", max_length=64)),
                ("safe_detail", models.CharField(blank=True, default="", max_length=255)),
                ("retry_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("cupom", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name="disponibilidades",
                                             to="scrapers.cupomnormalizado")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                    related_name="disponibilidades_cupons",
                                                    to="accounts.organization")),
                ("usuario", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                               related_name="disponibilidades_cupons",
                                               to=settings.AUTH_USER_MODEL)),
            ],
            options={"constraints": [models.UniqueConstraint(
                fields=("organization", "usuario", "cupom", "channel", "use_mode"),
                name="uniq_coupon_readiness_projection")]},
        ),
        migrations.CreateModel(
            name="CupomDisponibilidadeEvento",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("from_stage", models.CharField(blank=True, default="", max_length=24)),
                ("to_stage", models.CharField(max_length=24)),
                ("category", models.CharField(blank=True, default="", max_length=32)),
                ("reason_code", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("disponibilidade", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name="eventos",
                    to="scrapers.cupomdisponibilidade")),
                ("organization", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="eventos_disponibilidade_cupom",
                    to="accounts.organization")),
            ],
        ),
        migrations.CreateModel(
            name="CupomFonteObservacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("canonical_key", models.CharField(db_index=True, max_length=220)),
                ("source_external_id", models.CharField(blank=True, default="", max_length=180)),
                ("precedence", models.PositiveSmallIntegerField(default=100)),
                ("health_status", models.CharField(db_index=True, default="unknown", max_length=24)),
                ("outcome", models.CharField(db_index=True, default="accepted", max_length=32)),
                ("reason_code", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("evidence", models.JSONField(blank=True, default=dict)),
                ("observed_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("cupom", models.ForeignKey(blank=True, null=True,
                                             on_delete=django.db.models.deletion.CASCADE,
                                             related_name="observacoes_fontes",
                                             to="scrapers.cupomnormalizado")),
                ("fonte", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name="observacoes_cupons",
                                             to="scrapers.fonteingestao")),
                ("organization", models.ForeignKey(blank=True, null=True,
                                                    on_delete=django.db.models.deletion.CASCADE,
                                                    related_name="observacoes_fontes_cupom",
                                                    to="accounts.organization")),
            ],
            options={"constraints": [
                models.UniqueConstraint(
                    condition=models.Q(("organization__isnull", True)),
                    fields=("fonte", "canonical_key", "source_external_id"),
                    name="uniq_public_coupon_source_observation",
                ),
                models.UniqueConstraint(
                    condition=models.Q(("organization__isnull", False)),
                    fields=(
                        "organization", "fonte", "canonical_key",
                        "source_external_id",
                    ),
                    name="uniq_tenant_coupon_source_observation",
                ),
            ]},
        ),
        migrations.CreateModel(
            name="PublicacaoEvento",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("stage", models.CharField(db_index=True, max_length=32)),
                ("reason_code", models.CharField(blank=True, default="", max_length=64)),
                ("safe_detail", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                    related_name="eventos_publicacao",
                                                    to="accounts.organization")),
                ("publicacao", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                  related_name="eventos_estado",
                                                  to="scrapers.publicacao")),
            ],
        ),
        migrations.CreateModel(
            name="PublicacaoTentativa",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("numero", models.PositiveSmallIntegerField()),
                ("stage", models.CharField(db_index=True, max_length=32)),
                ("classification", models.CharField(blank=True, default="", max_length=24)),
                ("result", models.CharField(blank=True, default="", max_length=32)),
                ("reason_code", models.CharField(blank=True, default="", max_length=64)),
                ("duration_ms", models.PositiveIntegerField(default=0)),
                ("next_retry_at", models.DateTimeField(blank=True, null=True)),
                ("started_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                    related_name="tentativas_publicacao",
                                                    to="accounts.organization")),
                ("publicacao", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                  related_name="tentativas",
                                                  to="scrapers.publicacao")),
            ],
            options={"constraints": [models.UniqueConstraint(
                fields=("publicacao", "numero"), name="uniq_publication_attempt")]},
        ),
        migrations.CreateModel(
            name="ResourceLease",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("resource_key", models.CharField(max_length=180, unique=True)),
                ("owner_token", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("owner_kind", models.CharField(blank=True, default="", max_length=40)),
                ("acquired_at", models.DateTimeField(blank=True, null=True)),
                ("heartbeat_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("consecutive_manual", models.PositiveSmallIntegerField(default=0)),
                ("manual_waiting_since", models.DateTimeField(blank=True, null=True)),
                ("scheduled_waiting_since", models.DateTimeField(blank=True, null=True)),
                ("organization", models.ForeignKey(blank=True, null=True,
                                                    on_delete=django.db.models.deletion.SET_NULL,
                                                    related_name="resource_leases",
                                                    to="accounts.organization")),
            ],
        ),
        migrations.RunPython(backfill_publication_state, noop_reverse),
    ]
