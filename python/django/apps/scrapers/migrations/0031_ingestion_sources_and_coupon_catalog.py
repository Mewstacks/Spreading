from django.db import migrations, models
import django.db.models.deletion


PRODUCT_FIELDS = {
    "confianca": lambda: models.CharField(db_index=True, default="media", max_length=20),
    "evidencia": lambda: models.JSONField(blank=True, default=dict),
    "valido_ate": lambda: models.DateTimeField(blank=True, db_index=True, null=True),
    "falhas_consecutivas": lambda: models.PositiveIntegerField(default=0),
}


def add_missing_product_fields(apps, schema_editor):
    """Compatibilidade com produção, que já possuía valido_ate fora do histórico."""
    Produto = apps.get_model("scrapers", "Produto")
    table = Produto._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        existing = {
            column.name for column in
            schema_editor.connection.introspection.get_table_description(cursor, table)
        }
    for name, factory in PRODUCT_FIELDS.items():
        if name in existing:
            continue
        field = factory()
        Produto.add_to_class(name, field)
        schema_editor.add_field(Produto, Produto._meta.get_field(name))


def backfill(apps, schema_editor):
    Fonte = apps.get_model("scrapers", "FonteIngestao")
    Produto = apps.get_model("scrapers", "Produto")
    ml, _ = Fonte.objects.get_or_create(slug="mercadolivre-web", defaults={
        "marketplace": "mercadolivre", "nome": "Mercado Livre — páginas públicas",
        "status": "degraded",
    })
    az, _ = Fonte.objects.get_or_create(slug="amazon-creators-api", defaults={
        "marketplace": "amazon", "nome": "Amazon Creators API", "status": "degraded",
    })
    Fonte.objects.get_or_create(slug="amazon-public-web", defaults={
        "marketplace": "amazon", "nome": "Amazon — catálogo público", "status": "degraded",
    })
    for slug, nome in (("promobit-community", "Promobit (experimental)"),
                       ("pelando-community", "Pelando (experimental)"),
                       ("licensed-affiliate-feed", "Feed licenciado de afiliados")):
        Fonte.objects.get_or_create(slug=slug, defaults={
            "marketplace": "multiloja", "nome": nome, "habilitada": False,
            "status": "disabled"})
    Produto.objects.filter(marketplace="mercadolivre", fonte="").update(fonte=ml.slug)
    Produto.objects.filter(marketplace="amazon", fonte="").update(fonte=az.slug)


class Migration(migrations.Migration):
    dependencies = [("scrapers", "0030_remove_legacy_historicoenvio_chave")]
    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(
                add_missing_product_fields, migrations.RunPython.noop)],
            state_operations=[
                migrations.AddField(model_name="produto", name="confianca", field=models.CharField(db_index=True, default="media", max_length=20)),
                migrations.AddField(model_name="produto", name="evidencia", field=models.JSONField(blank=True, default=dict)),
                migrations.AddField(model_name="produto", name="valido_ate", field=models.DateTimeField(blank=True, db_index=True, null=True)),
                migrations.AddField(model_name="produto", name="falhas_consecutivas", field=models.PositiveIntegerField(default=0)),
            ],
        ),
        migrations.CreateModel(name="FonteIngestao", fields=[("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")), ("slug", models.CharField(max_length=80, unique=True)), ("marketplace", models.CharField(db_index=True, max_length=20)), ("nome", models.CharField(max_length=120)), ("habilitada", models.BooleanField(default=True)), ("status", models.CharField(choices=[("ok", "ok"), ("degraded", "degraded"), ("blocked", "blocked"), ("disabled", "disabled")], default="degraded", max_length=20)), ("ultimo_sucesso", models.DateTimeField(blank=True, null=True)), ("ultima_tentativa", models.DateTimeField(blank=True, null=True)), ("ultimo_total", models.PositiveIntegerField(default=0)), ("erro_publico", models.CharField(blank=True, default="", max_length=255)), ("falhas_consecutivas", models.PositiveIntegerField(default=0))]),
        migrations.CreateModel(name="ExecucaoIngestao", fields=[("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")), ("iniciada_em", models.DateTimeField(auto_now_add=True, db_index=True)), ("finalizada_em", models.DateTimeField(blank=True, null=True)), ("status", models.CharField(choices=[("running", "running"), ("ok", "ok"), ("empty", "empty"), ("error", "error"), ("blocked", "blocked")], default="running", max_length=20)), ("total_ofertas", models.PositiveIntegerField(default=0)), ("total_cupons", models.PositiveIntegerField(default=0)), ("erro_publico", models.CharField(blank=True, default="", max_length=255)), ("fonte", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="execucoes", to="scrapers.fonteingestao"))]),
        migrations.CreateModel(name="CupomNormalizado", fields=[("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")), ("external_id", models.CharField(max_length=160)), ("marketplace", models.CharField(db_index=True, max_length=20)), ("titulo", models.CharField(max_length=255)), ("codigo", models.CharField(blank=True, default="", max_length=120)), ("regras", models.JSONField(blank=True, default=dict)), ("link", models.URLField(blank=True, default="", max_length=1000)), ("validade", models.DateTimeField(blank=True, db_index=True, null=True)), ("estado", models.CharField(db_index=True, default="ativo", max_length=20)), ("confianca", models.CharField(db_index=True, default="baixa", max_length=20)), ("evidencia", models.JSONField(blank=True, default=dict)), ("primeira_observacao", models.DateTimeField(auto_now_add=True)), ("ultima_observacao", models.DateTimeField(auto_now=True)), ("fonte", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="cupons", to="scrapers.fonteingestao"))], options={"unique_together": {("fonte", "external_id")}}),
        migrations.CreateModel(name="ProdutoCupom", fields=[("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")), ("status", models.CharField(choices=[("confirmado", "Confirmado"), ("provavel", "Provável"), ("nao_aplicavel", "Não aplicável"), ("expirado", "Expirado")], default="provavel", max_length=20)), ("verificado_em", models.DateTimeField(blank=True, null=True)), ("evidencia", models.JSONField(blank=True, default=dict)), ("cupom", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="produtos", to="scrapers.cupomnormalizado")), ("produto", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="cupons_normalizados", to="scrapers.produto"))], options={"unique_together": {("produto", "cupom")}}),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
