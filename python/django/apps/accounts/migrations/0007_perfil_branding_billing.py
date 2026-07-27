from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0006_merge_20260705_1538")]

    operations = [
        migrations.AddField(model_name="perfil", name="nome_marca", field=models.CharField(default="Ofertas", max_length=80)),
        migrations.AddField(model_name="perfil", name="tom_marca", field=models.CharField(default="direto", max_length=20)),
        migrations.AddField(model_name="perfil", name="nivel_emoji", field=models.PositiveSmallIntegerField(default=2)),
        migrations.AddField(model_name="perfil", name="chamada_acao", field=models.CharField(default="Compre aqui", max_length=120)),
        migrations.AddField(model_name="perfil", name="divulgacao_afiliado", field=models.CharField(default="Este conteúdo contém link de afiliado.", max_length=180)),
        migrations.AddField(model_name="perfil", name="template_a", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="perfil", name="template_b", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="perfil", name="plano", field=models.CharField(default="piloto", max_length=20)),
        migrations.AddField(model_name="perfil", name="assinatura_status", field=models.CharField(default="trial", max_length=20)),
        migrations.AddField(model_name="perfil", name="trial_termina_em", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="perfil", name="billing_customer_id", field=models.CharField(blank=True, default="", max_length=120)),
        migrations.AddField(model_name="perfil", name="billing_subscription_id", field=models.CharField(blank=True, default="", max_length=120)),
    ]
