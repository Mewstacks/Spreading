from django.conf import settings

from .models import WhatsAppConnection, organization_for_user


def enabled_for_user(flag_name: str, user=None) -> bool:
    if not getattr(settings, flag_name, False):
        return False
    allowlist = settings.PILOT_ORGANIZATION_IDS
    if not allowlist:
        return True
    organization = organization_for_user(user)
    return bool(organization and str(organization.pk) in allowlist)


def enabled_for_whatsapp_session(session_id: str) -> bool:
    """Resolve o piloto pelo vínculo servidor->tenant, nunca por dado do cliente."""
    if not settings.WHATSAPP_WEB_ENABLED:
        return False
    allowlist = settings.PILOT_ORGANIZATION_IDS
    if not allowlist:
        return True
    organization_id = WhatsAppConnection.objects.filter(
        instance_id=str(session_id or ""),
        organization__status="active",
    ).values_list("organization_id", flat=True).first()
    return bool(organization_id and str(organization_id) in allowlist)
