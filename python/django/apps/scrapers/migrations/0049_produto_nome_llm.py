from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scrapers", "0048_cupom_preparacao_e_precos"),
    ]

    operations = [
        migrations.AddField(
            model_name="produto",
            name="nome_llm",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
    ]
