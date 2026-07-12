from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scrapers", "0026_attribution_and_offer_trust"),
    ]

    operations = [
        migrations.AddField(
            model_name="cupom",
            name="fonte",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
        migrations.AddField(
            model_name="cupom",
            name="validade",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="cupom",
            name="ultima_verificacao",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="cupom",
            name="estado",
            field=models.CharField(db_index=True, default="ativo", max_length=20),
        ),
    ]
