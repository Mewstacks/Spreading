from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

import apps.accounts.fields


def seed_manual_source(apps, schema_editor):
    Fonte = apps.get_model("scrapers", "FonteIngestao")
    Fonte.objects.get_or_create(
        slug="manual-private",
        defaults={
            "marketplace": "multiloja",
            "nome": "Cupons privados do afiliado",
            "habilitada": True,
            "status": "ok",
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("scrapers", "0046_produto_relampago"),
    ]

    operations = [
        migrations.CreateModel(
            name="IntegracaoAfiliado",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("provedor", models.CharField(db_index=True, default="awin", max_length=30)),
                ("identificador_conta", models.CharField(blank=True, default="", max_length=120)),
                ("nome_conta", models.CharField(blank=True, default="", max_length=160)),
                ("token", apps.accounts.fields.EncryptedCharField(blank=True, default="", max_length=4096)),
                ("habilitada", models.BooleanField(default=True)),
                ("status", models.CharField(choices=[
                    ("pendente", "Pendente"), ("conectada", "Conectada"),
                    ("degradada", "Degradada"), ("reconectar", "Reconectar"),
                    ("desativada", "Desativada")], db_index=True,
                    default="pendente", max_length=20)),
                ("ultima_tentativa", models.DateTimeField(blank=True, null=True)),
                ("ultimo_sucesso", models.DateTimeField(blank=True, null=True)),
                ("proxima_sincronizacao", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("programas_sincronizados_em", models.DateTimeField(blank=True, null=True)),
                ("erro_publico", models.CharField(blank=True, default="", max_length=255)),
                ("falhas_consecutivas", models.PositiveIntegerField(default=0)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name="integracoes_afiliado",
                                             to=settings.AUTH_USER_MODEL)),
            ],
            options={"constraints": [models.UniqueConstraint(
                fields=("owner", "provedor"), name="uniq_integracao_provedor_usuario")]},
        ),
        migrations.CreateModel(
            name="ProgramaAfiliado",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("external_id", models.CharField(max_length=80)),
                ("nome", models.CharField(max_length=180)),
                ("dominio", models.CharField(blank=True, default="", max_length=255)),
                ("dominios_validos", models.JSONField(blank=True, default=list)),
                ("logo_url", models.URLField(blank=True, default="", max_length=1000)),
                ("status_vinculo", models.CharField(db_index=True, default="joined", max_length=30)),
                ("link_status", models.CharField(db_index=True, default="online", max_length=30)),
                ("deeplink_habilitado", models.BooleanField(default=True)),
                ("habilitado", models.BooleanField(default=True)),
                ("comissao_min", models.FloatField(blank=True, null=True)),
                ("comissao_max", models.FloatField(blank=True, null=True)),
                ("comissao_tipo", models.CharField(blank=True, default="", max_length=20)),
                ("comissao_sincronizada_em", models.DateTimeField(blank=True, null=True)),
                ("atualizado_em", models.DateTimeField(auto_now=True)),
                ("integracao", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                  related_name="programas",
                                                  to="scrapers.integracaoafiliado")),
            ],
            options={"constraints": [models.UniqueConstraint(
                fields=("integracao", "external_id"), name="uniq_programa_por_integracao")]},
        ),
        migrations.AddField(
            model_name="execucaoingestao", name="integracao",
            field=models.ForeignKey(blank=True, null=True,
                                    on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="execucoes", to="scrapers.integracaoafiliado"),
        ),
        migrations.AlterUniqueTogether(name="cupomnormalizado", unique_together=set()),
        migrations.AddField(
            model_name="cupomnormalizado", name="owner",
            field=models.ForeignKey(blank=True, null=True, db_index=True,
                                    on_delete=django.db.models.deletion.CASCADE,
                                    related_name="cupons_normalizados", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="cupomnormalizado", name="integracao",
            field=models.ForeignKey(blank=True, null=True,
                                    on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="cupons", to="scrapers.integracaoafiliado"),
        ),
        migrations.AddField(
            model_name="cupomnormalizado", name="programa",
            field=models.ForeignKey(blank=True, null=True,
                                    on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="cupons", to="scrapers.programaafiliado"),
        ),
        migrations.AddField(model_name="cupomnormalizado", name="tipo_conteudo",
                            field=models.CharField(db_index=True, default="voucher", max_length=20)),
        migrations.AddField(model_name="cupomnormalizado", name="anunciante_nome",
                            field=models.CharField(blank=True, default="", max_length=180)),
        migrations.AddField(model_name="cupomnormalizado", name="inicio",
                            field=models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField(model_name="cupomnormalizado", name="restrito",
                            field=models.BooleanField(db_index=True, default=False)),
        migrations.AddField(model_name="cupomnormalizado", name="relampago",
                            field=models.BooleanField(db_index=True, default=False)),
        migrations.AddConstraint(
            model_name="cupomnormalizado",
            constraint=models.UniqueConstraint(
                condition=models.Q(owner__isnull=True), fields=("fonte", "external_id"),
                name="uniq_cupom_compartilhado_fonte_external"),
        ),
        migrations.AddConstraint(
            model_name="cupomnormalizado",
            constraint=models.UniqueConstraint(
                condition=models.Q(owner__isnull=False),
                fields=("owner", "fonte", "external_id"),
                name="uniq_cupom_privado_owner_fonte_external"),
        ),
        migrations.AddField(model_name="configuracaoenvio", name="incluir_restritos",
                            field=models.BooleanField(default=True)),
        migrations.AddField(model_name="configuracaoenvio", name="incluir_sem_desconto",
                            field=models.BooleanField(default=True)),
        migrations.AddField(
            model_name="configuracaoenvio", name="programas",
            field=models.ManyToManyField(blank=True, related_name="configuracoes",
                                         to="scrapers.programaafiliado"),
        ),
        migrations.RunPython(seed_manual_source, migrations.RunPython.noop),
    ]
