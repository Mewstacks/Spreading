from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0018_mercadolivresession_linkbuilder_probe"),
    ]

    operations = [
        migrations.AddField(
            model_name="mercadolivresession",
            name="lb_readiness",
            field=models.CharField(
                choices=[
                    ("unknown", "Não verificado"),
                    ("ready", "Pronto"),
                    ("login_required", "Login necessário"),
                    ("temporarily_unavailable", "Temporariamente indisponível"),
                ],
                db_index=True,
                default="unknown",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="mercadolivresession",
            name="lb_readiness_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="mercadolivresession",
            name="lb_readiness_reason",
            field=models.CharField(blank=True, max_length=200),
        ),
    ]
