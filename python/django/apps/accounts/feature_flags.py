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
# com efeito externo para todas as contas. Só o pipeline de envio v2 (efeitos
# externos reais) continua exigindo rollout explícito; cupons de ativação e
# relatórios da Amazon são funcionalidades básicas e nascem liberadas para todos.
_EXPLICIT_ROLLOUT_FLAGS = frozenset({
    "SEND_PIPELINE_V2_ENABLED",
})


def _no_tenant(fn, *args, **kwargs):
    from .tenant import executar_orm_ou_direto
    return executar_orm_ou_direto(fn, *args, **kwargs)


def _decisao_memorizada(flag_name, user):
    """Decide a flag no MÁXIMO uma vez por instância de usuário.

    `organization_for_user` custa 2 queries e o override custa mais uma. A tela de
    Promoções pergunta ML_CUPONS_ATIVACAO_ENABLED uma vez POR CUPOM (`score_cupom`
    -> `ativacao_publicavel`), então um catálogo de ~5.800 cupons virava mais de mil
    idas ao banco por GET — era daí que vinha a maior parte da lentidão da tela.

    O escopo do memo é a instância de `user`, como o `_perm_cache` do próprio Django:
    a request web carrega um usuário novo a cada vez, e um job SSE decide a flag uma
    vez e mantém a MESMA resposta do início ao fim — que é o comportamento desejado
    para uma decisão de rollout no meio de um envio.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return _feature_decision(flag_name, None)
    try:
        memo = user._feature_flag_cache
    except AttributeError:
        memo = {}
        try:
            user._feature_flag_cache = memo
        except AttributeError:
            # Usuário imutável/proxy: segue sem memo, só mais lento.
            memo = None
    if memo is not None and flag_name in memo:
        return memo[flag_name]
    organization = _no_tenant(organization_for_user, user)
    decisao = _feature_decision(flag_name, organization)
    if memo is not None:
        memo[flag_name] = decisao
    return decisao


def enabled_for_user(flag_name: str, user=None) -> bool:
    return _decisao_memorizada(flag_name, user)[0]


def feature_decision(flag_name: str, user=None) -> tuple[bool, str]:
    """Retorna decisão e motivo estável, sem expor configuração sensível."""
    return _decisao_memorizada(flag_name, user)


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
