import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("scrapers", "0025_alter_cupom_link_original_alter_produto_link_produto"),
    ]

    operations = [
        migrations.AddField(model_name="produto", name="fonte", field=models.CharField(blank=True, default="", max_length=80)),
        migrations.AddField(model_name="produto", name="primeira_observacao", field=models.DateTimeField(auto_now_add=True, null=True)),
        migrations.AddField(model_name="produto", name="ultima_observacao", field=models.DateTimeField(auto_now=True, null=True)),
        migrations.AddField(model_name="produto", name="ultima_verificacao", field=models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField(model_name="produto", name="estado", field=models.CharField(db_index=True, default="ativo", max_length=20)),
        migrations.AddField(model_name="produto", name="falha_verificacao", field=models.CharField(blank=True, default="", max_length=255)),
        migrations.AddField(model_name="produto", name="preco_fonte", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="produto", name="preco_efetivo", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="configuracaoenvio", name="max_envios_dia", field=models.PositiveIntegerField(default=20)),
        migrations.AddField(model_name="configuracaoenvio", name="falhas_consecutivas", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="configuracaoenvio", name="pausar_apos_falhas", field=models.PositiveIntegerField(default=5)),
        migrations.AddField(model_name="configuracaoenvio", name="motivo_pausa", field=models.CharField(blank=True, default="", max_length=255)),
        migrations.AddField(model_name="configuracaoenvio", name="variante_template", field=models.CharField(default="alternar", max_length=10)),
        migrations.AddField(model_name="configuracaoenvio", name="nome_marca", field=models.CharField(blank=True, default="", max_length=80)),
        migrations.AddField(model_name="configuracaoenvio", name="tom_marca", field=models.CharField(blank=True, default="", max_length=20)),
        migrations.AddField(model_name="configuracaoenvio", name="nivel_emoji", field=models.PositiveSmallIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="configuracaoenvio", name="chamada_acao", field=models.CharField(blank=True, default="", max_length=120)),
        migrations.AddField(model_name="configuracaoenvio", name="divulgacao_afiliado", field=models.CharField(blank=True, default="", max_length=180)),
        migrations.AddField(model_name="configuracaoenvio", name="template_a", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="configuracaoenvio", name="template_b", field=models.TextField(blank=True, default="")),
        migrations.CreateModel(
            name="Publicacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("id_publico", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("canal", models.CharField(max_length=20)),
                ("destino_id", models.CharField(max_length=100)),
                ("destino_nome", models.CharField(blank=True, default="", max_length=255)),
                ("status", models.CharField(choices=[("pendente", "Pendente"), ("enviado", "Enviado"), ("falhou", "Falhou"), ("ignorado", "Ignorado")], db_index=True, default="pendente", max_length=20)),
                ("erro", models.CharField(blank=True, default="", max_length=500)),
                ("variante", models.CharField(default="A", max_length=1)),
                ("mensagem", models.TextField(blank=True, default="")),
                ("link_afiliado", models.URLField(blank=True, default="", max_length=1500)),
                ("link_rastreado", models.URLField(blank=True, default="", max_length=1500)),
                ("preco_original", models.FloatField(default=0)),
                ("preco_final", models.FloatField(default=0)),
                ("cupom", models.CharField(blank=True, default="", max_length=255)),
                ("categoria", models.CharField(blank=True, default="", max_length=100)),
                ("score", models.FloatField(default=0)),
                ("motivos_score", models.JSONField(blank=True, default=list)),
                ("criada_em", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("enviada_em", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("configuracao", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="publicacoes", to="scrapers.configuracaoenvio")),
                ("produto", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="publicacoes", to="scrapers.produto")),
                ("usuario", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="publicacoes", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="CliquePublicacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("clicado_em", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("publicacao", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="cliques", to="scrapers.publicacao")),
            ],
        ),
        migrations.CreateModel(
            name="ReceitaAfiliado",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("marketplace", models.CharField(db_index=True, max_length=20)),
                ("data", models.DateField(db_index=True)),
                ("etiqueta", models.CharField(blank=True, default="", max_length=120)),
                ("produto_nome", models.CharField(blank=True, default="", max_length=255)),
                ("pedidos", models.PositiveIntegerField(default=0)),
                ("receita", models.FloatField(default=0)),
                ("comissao", models.FloatField(default=0)),
                ("hash_origem", models.CharField(max_length=64, unique=True)),
                ("importada_em", models.DateTimeField(auto_now_add=True)),
                ("usuario", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="receitas_afiliado", to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
