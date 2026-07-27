import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0016_perfil_organization"),
        ("scrapers", "0052_backfill_organizations"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExecucaoRaspagem",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ("tipo", models.CharField(
                    choices=[("ofertas", "Promoções"), ("cupons", "Cupons")],
                    db_index=True, max_length=16)),
                ("status", models.CharField(
                    choices=[
                        ("queued", "Na fila"), ("running", "Executando"),
                        ("succeeded", "Concluída"),
                        ("partial", "Concluída parcialmente"),
                        ("failed", "Falhou"),
                    ],
                    db_index=True, default="queued", max_length=16)),
                ("etapa", models.CharField(blank=True, default="", max_length=80)),
                ("progresso", models.PositiveSmallIntegerField(default=0)),
                ("contagens", models.JSONField(blank=True, default=dict)),
                ("codigo_erro", models.CharField(blank=True, default="", max_length=40)),
                ("erro_publico", models.CharField(blank=True, default="", max_length=255)),
                ("acao_recomendada", models.CharField(blank=True, default="", max_length=255)),
                ("tentativas", models.PositiveSmallIntegerField(default=0)),
                ("criada_em", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("iniciada_em", models.DateTimeField(blank=True, null=True)),
                ("heartbeat_em", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("finalizada_em", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("organization", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="execucoes_raspagem", to="accounts.organization")),
                ("solicitada_por", models.ForeignKey(
                    null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="raspagens_solicitadas", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-criada_em",)},
        ),
        migrations.CreateModel(
            name="EventoRaspagem",
            fields=[
                ("id", models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False,
                    verbose_name="ID")),
                ("nivel", models.CharField(
                    choices=[
                        ("info", "Informação"), ("warning", "Aviso"),
                        ("error", "Erro"), ("success", "Sucesso"),
                    ],
                    default="info", max_length=12)),
                ("etapa", models.CharField(blank=True, default="", max_length=80)),
                ("mensagem", models.CharField(max_length=500)),
                ("progresso", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("criado_em", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("execucao", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="eventos", to="scrapers.execucaoraspagem")),
                ("organization", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="eventos_raspagem", to="accounts.organization")),
            ],
            options={"ordering": ("id",)},
        ),
        migrations.AddConstraint(
            model_name="execucaoraspagem",
            constraint=models.UniqueConstraint(
                condition=models.Q(status__in=("queued", "running")),
                fields=("organization",),
                name="uniq_raspagem_manual_ativa_por_org",
            ),
        ),
        migrations.AddIndex(
            model_name="execucaoraspagem",
            index=models.Index(
                fields=["status", "criada_em"],
                name="scrape_job_status_created"),
        ),
        migrations.AddIndex(
            model_name="eventoraspagem",
            index=models.Index(
                fields=["execucao", "id"],
                name="scrape_event_job_cursor"),
        ),
    ]
