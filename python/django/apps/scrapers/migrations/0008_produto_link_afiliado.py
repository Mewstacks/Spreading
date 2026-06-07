from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scrapers', '0007_add_valor_minimo'),
    ]

    operations = [
        migrations.AddField(
            model_name='produto',
            name='url_isca',
            field=models.URLField(blank=True, default='', max_length=1000),
        ),
        migrations.AddField(
            model_name='produto',
            name='link_afiliado',
            field=models.URLField(blank=True, default='', max_length=1000),
        ),
    ]
