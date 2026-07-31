from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scrapers', '0016_configuracaoenvio_janela_fim_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='produto',
            name='asin',
            field=models.CharField(blank=True, db_index=True, default='', max_length=20),
        ),
    ]
