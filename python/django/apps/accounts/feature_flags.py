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

from .models import (
    OrganizationFeatureOverride, WhatsAppConnection, organization_for_user,
)


# Ligar a setting global destes recursos apenas abre a possibilidade de rollout.
# Uma organização ainda precisa de override ``enabled`` ou estar na allowlist.
# Isso evita que uma lista de pilotos acidentalmente vazia libere comportamento
# com efeito externo para todas as contas.
_EXPLICIT_ROLLOUT_FLAGS = frozenset({
    "ML_CUPONS_ATIVACAO_ENABLED",
    "SEND_PIPELINE_V2_ENABLED",
    "AMAZON_BROWSER_REPORTS_ENABLED",
})


def _no_tenant(fn, *args, **kwargs):
    from .tenant import executar_orm_ou_direto
    return executar_orm_ou_direto(fn, *args, **kwargs)


def enabled_for_user(flag_name: str, user=None) -> bool:
    organization = _no_tenant(organization_for_user, user)
    return _feature_decision(flag_name, organization)[0]


def feature_decision(flag_name: str, user=None) -> tuple[bool, str]:
    """Retorna decisão e motivo estável, sem expor configuração sensível."""
    organization = _no_tenant(organization_for_user, user)
    return _feature_decision(flag_name, organization)


def _feature_decision(flag_name, organization):
    if not getattr(settings, flag_name, False):
        return False, "global_kill_switch"

    def _override():
        if organization is None:
            return "inherit"
        return (
            OrganizationFeatureOverride.objects.filter(
                organization=organization, feature=flag_name,
            ).values_list("state", flat=True).first() or "inherit"
        )

    state = _no_tenant(_override)
    if state == "disabled":
        return False, "organization_disabled"
    if state == "enabled":
        return True, "organization_enabled"
    allowlist = settings.PILOT_ORGANIZATION_IDS
    if not allowlist:
        # Recursos com publicação/browser nunca abrem globalmente apenas porque a
        # allowlist ficou vazia. Exigem override explícito ou organização piloto.
        enabled = flag_name not in _EXPLICIT_ROLLOUT_FLAGS
        return enabled, "default_enabled" if enabled else "pilot_required"
    enabled = bool(organization and str(organization.pk) in allowlist)
    return enabled, "pilot_enabled" if enabled else "pilot_required"


def send_pipeline_v2_enabled(user=None) -> bool:
    """Gate tenant-aware do pipeline que produz efeitos externos."""
    return enabled_for_user("SEND_PIPELINE_V2_ENABLED", user)


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
