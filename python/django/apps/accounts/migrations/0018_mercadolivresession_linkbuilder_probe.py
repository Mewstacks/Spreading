from django.db import migrations, models


class Migration(migrations.Migration):
    """Veredito da sonda do Link Builder, ao lado do veredito do site principal.

    Todos nuláveis/com default: nenhuma sessão existente é invalidada e não há
    backfill — a primeira sonda de cada organização preenche as colunas.
    """

    dependencies = [
        ("accounts", "0017_mercadolivresession_last_probe_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="mercadolivresession",
            name="lb_last_probe_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="mercadolivresession",
            name="lb_last_probe_result",
            field=models.CharField(
                blank=True,
                choices=[
                    ("conectado", "Conectado"),
                    ("suspeito", "Suspeito"),
                    ("inconclusivo", "Inconclusivo"),
                ],
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="mercadolivresession",
            name="lb_probe_failures",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="mercadolivresession",
            name="lb_probe_reason",
            field=models.CharField(blank=True, max_length=200),
        ),
    ]
