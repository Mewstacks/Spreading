import uuid

from django.db import migrations
from django.utils.text import slugify


def backfill(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Organization = apps.get_model("accounts", "Organization")
    Membership = apps.get_model("accounts", "Membership")

    for user in User.objects.all().iterator():
        organization = Organization.objects.filter(personal_owner_id=user.pk).first()
        if organization is None:
            base = slugify(user.username)[:80] or "conta"
            full_name = " ".join(
                value.strip()
                for value in (user.first_name or "", user.last_name or "")
                if value.strip()
            )
            organization = Organization.objects.create(
                id=uuid.uuid4(),
                name=(full_name or user.username or f"Conta {user.pk}"),
                slug=f"{base}-{user.pk}-{uuid.uuid4().hex[:6]}",
                status="active",
                personal_owner_id=user.pk,
            )
        Membership.objects.get_or_create(
            organization_id=organization.pk,
            user_id=user.pk,
            defaults={"role": "owner", "is_active": True},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0011_organization_membership"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
