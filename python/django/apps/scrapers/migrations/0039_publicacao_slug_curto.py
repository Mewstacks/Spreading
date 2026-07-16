import apps.scrapers.models
from django.db import migrations, models


class Migration(migrations.Migration):
    """Slug curto do link rastreado (/r/<slug>/).

    Em dois passos de propósito: AddField com default aplicaria o MESMO slug
    (o callable roda uma vez no DDL) a todas as linhas existentes e estouraria
    o unique. Linhas antigas ficam NULL — o token assinado antigo segue
    resolvendo os links já publicados — e só as novas ganham slug.
    """

    dependencies = [
        ("scrapers", "0038_eventooperacional_incidente_processado"),
    ]

    operations = [
        migrations.AddField(
            model_name="publicacao",
            name="slug_curto",
            field=models.CharField(blank=True, editable=False, max_length=12, null=True),
        ),
        migrations.AlterField(
            model_name="publicacao",
            name="slug_curto",
            field=models.CharField(
                blank=True,
                default=apps.scrapers.models.gerar_slug_curto,
                editable=False,
                max_length=12,
                null=True,
                unique=True,
            ),
        ),
    ]
