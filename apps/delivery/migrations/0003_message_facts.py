# Identidade do que o grupo viu: preenche o histórico a partir do texto enviado.
import re
import unicodedata

from django.db import migrations, models

BATCH = 500


def _slug(text):
    # Mesma regra de apps.offers.keys.slug_identity (a migração não importa código da aplicação).
    folded = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()[:140]


def identity_from_body(body):
    """Nome curto da primeira linha da legenda (`🛍️ *Nome*`), já como identidade."""
    match = re.search(r"\*(.+?)\*", (body or "").split("\n", 1)[0])
    return _slug(match.group(1)) if match else ""


def backfill(apps, schema_editor):
    OfferDelivery = apps.get_model("delivery", "OfferDelivery")
    pending = []
    for row in OfferDelivery._default_manager.filter(identity="").select_related(
            "outbox_item").iterator():
        row.identity = identity_from_body(row.outbox_item.body)
        if row.identity:
            pending.append(row)
        if len(pending) >= BATCH:
            OfferDelivery._default_manager.bulk_update(pending, ["identity"])
            pending = []
    if pending:
        OfferDelivery._default_manager.bulk_update(pending, ["identity"])


class Migration(migrations.Migration):

    dependencies = [
        ("delivery", "0002_alert"),
    ]

    operations = [
        migrations.AddField(
            model_name="offerdelivery",
            name="identity",
            field=models.CharField(blank=True, default="", max_length=140),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
