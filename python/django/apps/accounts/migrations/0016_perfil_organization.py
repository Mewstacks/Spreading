from django.db import migrations, models
import django.db.models.deletion


def backfill_profile_organizations(apps, schema_editor):
    Organization = apps.get_model("accounts", "Organization")
    Perfil = apps.get_model("accounts", "Perfil")

    organizations = {
        organization.personal_owner_id: organization.pk
        for organization in Organization.objects.exclude(personal_owner_id=None)
    }
    missing = []
    for profile in Perfil.objects.only("pk", "user_id").iterator():
        organization_id = organizations.get(profile.user_id)
        if organization_id is None:
            missing.append(profile.pk)
            continue
        Perfil.objects.filter(pk=profile.pk).update(
            organization_id=organization_id,
        )
    if missing:
        raise RuntimeError(
            "Perfil sem Organization pessoal durante backfill: "
            + ", ".join(map(str, missing[:10]))
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0015_backfill_whatsapp_connections"),
    ]

    operations = [
        migrations.AddField(
            model_name="perfil",
            name="organization",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="profile_settings",
                to="accounts.organization",
            ),
        ),
        migrations.RunPython(
            backfill_profile_organizations,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="perfil",
            name="organization",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="profile_settings",
                to="accounts.organization",
            ),
        ),
    ]
