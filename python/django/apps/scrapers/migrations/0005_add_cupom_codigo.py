from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scrapers', '0004_add_macro_categoria'),
    ]

    operations = [
        migrations.AddField(
            model_name='cupom',
            name='codigo',
            field=models.CharField(blank=True, default='', max_length=512),
        ),
    ]
