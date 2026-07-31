from django.db import migrations


def backfill(apps, schema_editor):
    Organization = apps.get_model("accounts", "Organization")
    Perfil = apps.get_model("accounts", "Perfil")
    WhatsAppConnection = apps.get_model("accounts", "WhatsAppConnection")

    used = set(WhatsAppConnection.objects.values_list("instance_id", flat=True))
    for organization in Organization.objects.exclude(personal_owner_id=None):
        if WhatsAppConnection.objects.filter(organization_id=organization.pk).exists():
            continue
        profile = Perfil.objects.filter(user_id=organization.personal_owner_id).first()
        instance_id = (
            profile.wa_session if profile and profile.wa_session
            else str(organization.personal_owner_id)
        )
        if instance_id in used:
            raise RuntimeError(
                f"WhatsApp instance_id duplicado entre organizações: {instance_id}"
            )
        WhatsAppConnection.objects.create(
            organization_id=organization.pk,
            instance_id=instance_id,
            status="inactive",
        )
        used.add(instance_id)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0014_whatsappconnection"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]

