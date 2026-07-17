from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0009_remove_default_affiliate_disclosure")]
    operations = [
        migrations.AddField(model_name="perfil", name="amazon_relatorio_estado",
                            field=models.BooleanField(blank=True, null=True)),
        migrations.AddField(model_name="perfil", name="alerta_amazon_relatorio_em",
                            field=models.DateTimeField(blank=True, null=True)),
    ]
