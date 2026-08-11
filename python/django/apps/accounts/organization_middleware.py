"""Resolve a organização autorizada e instala o contexto RLS da request."""

import logging

from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.http import HttpResponse

from .models import Membership, Organization, Perfil, organization_for_user
from .tenant import actor_context, organization_context


logger = logging.getLogger(__name__)

_OWNER_ONLY_PREFIXES = (
    "/scrapers/conta/",
    "/scrapers/integracoes/",
    "/scrapers/ml/",
    "/scrapers/ml-relatorio/",
    "/scrapers/amazon/",
    "/scrapers/whatsapp/",
    "/scrapers/telegram/",
)
_MUTATING_GET_PREFIXES = (
    "/scrapers/run/",
    "/scrapers/gerar-links/",
    "/scrapers/ofertas/",
    "/scrapers/cupons-codigo/",
    "/scrapers/buscar-termo/",
    "/scrapers/enviar-agora/",
    "/scrapers/buscar-promocoes/",
)

# Resolver o tenant custa três consultas (Perfil, Organization, Membership) em TODA
# request autenticada. Na maior parte das telas isso se perde no ruído; no live view
# das conexões, não: cada clique e cada tecla é um POST próprio, e um POST que não
# toca o ORM estava pagando três idas ao Postgres antes de a view começar — dentro do
# mesmo processo que hospeda o Chromium do login. O vínculo de um usuário com sua
# organização não muda entre uma tecla e a seguinte.
#
# O TTL é curto E a chave é invalidada por signal: uma revogação de acesso vale na
# request seguinte, não daqui a 30 segundos. Sem a invalidação isto seria uma janela
# de autorização obsoleta, que é exatamente o que uma checagem de RBAC não pode ter.
_CACHE_TTL_S = 30
_CACHE_PREFIX = "orgctx:v1"


def _cache_key(user_pk) -> str:
    return f"{_CACHE_PREFIX}:{user_pk}"


def esquecer_contexto(user_pk) -> None:
    """Descarta a resolução cacheada de um usuário."""
    cache.delete(_cache_key(user_pk))


def _resolver(user):
    """(organization, membership) do usuário, servindo do cache quando possível.

    Devolve ``(None, None)`` quando não há organização ativa e
    ``(organization, None)`` quando há organização mas não há vínculo ativo — os
    dois casos que o middleware transforma em 403.

    A entrada guarda o ``date_joined`` junto e só é aceita se ele bater. A chave é a
    PK porque é só isso que os signals de invalidação têm em mãos, e PK sozinha não
    identifica uma conta: o rollback de cada ``TestCase`` devolve a sequência, então
    o usuário 1 de um teste não é o usuário 1 do seguinte. Sem essa conferência, um
    teste herdaria a organização do anterior — e em produção uma conta recriada com
    a PK reciclada herdaria o vínculo da antiga.
    """
    chave = _cache_key(user.pk)
    cached = cache.get(chave)
    if cached is not None and cached[0] == user.date_joined:
        return cached[1], cached[2]

    organization = organization_for_user(user)
    membership = None
    if organization is not None:
        membership = Membership.objects.filter(
            organization=organization, user=user, is_active=True,
        ).first()
    # Negativo também entra no cache: uma conta sem organização não pode virar três
    # consultas por request só porque o resultado é 403.
    cache.set(chave, (user.date_joined, organization, membership), _CACHE_TTL_S)
    return organization, membership


class OrganizationContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organization = None
        request.organization_membership = None
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return self.get_response(request)

        # O actor_context continua envolvendo tudo mesmo em cache hit: as policies de
        # RLS leem `app.actor_id` (apps/accounts/rls.py), então o GUC precisa estar
        # instalado na conexão desta request, tenha ou não havido consulta aqui.
        with actor_context(user.pk):
            organization, membership = _resolver(user)
            if organization is None:
                return HttpResponse(
                    "Conta sem organização ativa. Fale com o suporte.",
                    status=403,
                    content_type="text/plain; charset=utf-8",
                )
            if membership is None:
                return HttpResponse(
                    "Acesso à organização não autorizado.",
                    status=403,
                    content_type="text/plain; charset=utf-8",
                )

            request.organization = organization
            request.organization_membership = membership

            if not self._role_allows(request, membership):
                logger.warning(
                    "RBAC tenant negou request user=%s org=%s role=%s path=%s method=%s",
                    user.pk,
                    organization.pk,
                    membership.role,
                    request.path,
                    request.method,
                )
                return HttpResponse(
                    "Seu papel nesta organização não autoriza esta operação.",
                    status=403,
                    content_type="text/plain; charset=utf-8",
                )

            # O objeto User pertence exclusivamente a esta request. Guardar nele a
            # organização que acabou de passar por Membership/RBAC evita que cada
            # helper da mesma tela repita a resolução (Perfil + Organization +
            # Membership). O dashboard fazia isso para ML, Link Builder e WhatsApp,
            # multiplicando a latência intermitente do Postgres sem ganhar segurança.
            user._spreading_authorized_organization = organization

            # Nem superuser recebe bypass silencioso no processo web. Suporte
            # cross-tenant exige comando/worker com role dedicada e trilha própria.
            request.tenant_system_access = False
            with organization_context(organization):
                return self.get_response(request)

    @staticmethod
    def _role_allows(request, membership):
        role = membership.role
        path = request.path
        if role == "owner":
            return True
        if role == "operator":
            return not path.startswith(_OWNER_ONLY_PREFIXES)
        if role == "viewer":
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                return False
            if path.startswith(_OWNER_ONLY_PREFIXES):
                return False
            if path.startswith(_MUTATING_GET_PREFIXES):
                return False
            return True
        return False


@receiver(post_save, sender=Membership)
@receiver(post_delete, sender=Membership)
def _invalidar_por_membership(instance, **kwargs):
    esquecer_contexto(instance.user_id)


@receiver(post_save, sender=Perfil)
@receiver(post_delete, sender=Perfil)
def _invalidar_por_perfil(instance, **kwargs):
    # `active_organization` vive aqui: trocar de organização precisa valer já.
    esquecer_contexto(instance.user_id)


@receiver(post_save, sender=Organization)
def _invalidar_por_organizacao(instance, **kwargs):
    # Suspender uma organização precisa derrubar o acesso de TODOS os membros dela,
    # não só de quem por acaso mexeu no próprio vínculo.
    if instance.personal_owner_id:
        esquecer_contexto(instance.personal_owner_id)
    try:
        membros = list(
            Membership.objects.filter(organization=instance)
            .values_list("user_id", flat=True)
        )
    except Exception:
        # Um save vindo de um contexto que a RLS não deixa ler Membership não pode
        # falhar por causa da limpeza de cache. O TTL curto fecha a janela sozinho.
        logger.warning(
            "Não foi possível invalidar o contexto dos membros da organização %s.",
            instance.pk, exc_info=True,
        )
        return
    for user_id in membros:
        esquecer_contexto(user_id)
