from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("scrapers", "0059_hardening_control_plane"),
    ]

    operations = [
        migrations.AddField(
            model_name="publicacao",
            name="delivery_batch_key",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=64,
            ),
        ),
        migrations.AddField(
            model_name="publicacao",
            name="queued_media",
            field=models.BinaryField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="publicacao",
            name="queued_media_mime",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
    ]
