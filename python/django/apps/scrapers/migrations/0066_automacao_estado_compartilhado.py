from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("scrapers", "0065_reaplica_purga_com_contexto")]

    operations = [
        migrations.CreateModel(
            name="AutomacaoEstado",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("job", models.CharField(max_length=32, unique=True)),
                ("enabled", models.BooleanField(default=False)),
                ("configured", models.BooleanField(default=False)),
                ("state", models.JSONField(blank=True, default=dict)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
