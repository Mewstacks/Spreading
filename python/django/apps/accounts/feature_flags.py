"""Resolução das flags de piloto.

As duas funções daqui leem tabelas sob RLS (accounts_organization,
accounts_membership, accounts_whatsappconnection), e são chamadas tanto de dentro
de uma request (escopo instalado) quanto de dentro de um job SSE que roda com o
tenant apenas ANOTADO. No segundo caso a query nua volta zero linhas e a flag diz
"desligada para esta organização" — foi o que fez o envio recusar um WhatsApp e um
Mercado Livre perfeitamente conectados. Por isso toda ida ao banco passa por
`executar_orm_ou_direto`, que instala o escopo no SSE e cai no caminho direto no
worker de sistema.
"""
from django.conf import settings

from .models import WhatsAppConnection, organization_for_user


def _no_tenant(fn, *args, **kwargs):
    from .tenant import executar_orm_ou_direto
    return executar_orm_ou_direto(fn, *args, **kwargs)


def enabled_for_user(flag_name: str, user=None) -> bool:
    if not getattr(settings, flag_name, False):
        return False
    allowlist = settings.PILOT_ORGANIZATION_IDS
    if not allowlist:
        return True
    organization = _no_tenant(organization_for_user, user)
    return bool(organization and str(organization.pk) in allowlist)


def enabled_for_whatsapp_session(session_id: str) -> bool:
    """Resolve o piloto pelo vínculo servidor->tenant, nunca por dado do cliente."""
    if not settings.WHATSAPP_WEB_ENABLED:
        return False
    allowlist = settings.PILOT_ORGANIZATION_IDS
    if not allowlist:
        return True

    def _organization_id():
        return WhatsAppConnection.objects.filter(
            instance_id=str(session_id or ""),
            organization__status="active",
        ).values_list("organization_id", flat=True).first()

    organization_id = _no_tenant(_organization_id)
    return bool(organization_id and str(organization_id) in allowlist)
