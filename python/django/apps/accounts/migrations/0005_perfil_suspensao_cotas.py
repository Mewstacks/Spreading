from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_perfil_amazon_elegivel_perfil_amazon_ultimo_erro'),
    ]

    operations = [
        migrations.AddField(
            model_name='perfil',
            name='bloqueado',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='perfil',
            name='bloqueado_em',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='perfil',
            name='bloqueado_motivo',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='perfil',
            name='max_wa_sessions',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='perfil',
            name='max_configs',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='perfil',
            name='max_envios_dia',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
