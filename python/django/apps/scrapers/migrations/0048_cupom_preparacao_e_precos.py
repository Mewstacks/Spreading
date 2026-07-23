from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("scrapers", "0047_integracoes_awin_catalogo_privado"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="cupomnormalizado", name="produtos_chave",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="produtocupom", name="preco_original",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="produtocupom", name="preco_atual",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="produtocupom", name="preco_final",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.CreateModel(
            name="CupomPreparacao",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pendente", "Pendente"),
                                                     ("pronto", "Pronto"),
                                                     ("vazio", "Sem produtos"),
                                                     ("erro", "Erro")],
                                            db_index=True, default="pendente", max_length=20)),
                ("produtos_chave", models.CharField(blank=True, db_index=True,
                                                    default="", max_length=64)),
                ("verificado_em", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("proxima_tentativa", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("erro", models.CharField(blank=True, default="", max_length=500)),
                ("cupom", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name="preparacoes",
                                             to="scrapers.cupomnormalizado")),
                ("usuario", models.ForeignKey(blank=True, null=True,
                                               on_delete=django.db.models.deletion.CASCADE,
                                               related_name="cupons_preparados",
                                               to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name="cupompreparacao",
            constraint=models.UniqueConstraint(condition=models.Q(("usuario__isnull", True)),
                                                fields=("cupom",),
                                                name="uniq_preparo_cupom_compartilhado"),
        ),
        migrations.AddConstraint(
            model_name="cupompreparacao",
            constraint=models.UniqueConstraint(condition=models.Q(("usuario__isnull", False)),
                                                fields=("cupom", "usuario"),
                                                name="uniq_preparo_cupom_usuario"),
        ),
    ]
