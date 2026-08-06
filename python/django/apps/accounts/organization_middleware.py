"""Resolve a organização autorizada e instala o contexto RLS da request."""

import logging

from django.http import HttpResponse

from .models import Membership, organization_for_user
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


class OrganizationContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organization = None
        request.organization_membership = None
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return self.get_response(request)

        with actor_context(user.pk):
            organization = organization_for_user(user)
            if organization is None:
                return HttpResponse(
                    "Conta sem organização ativa. Fale com o suporte.",
                    status=403,
                    content_type="text/plain; charset=utf-8",
                )
            membership = Membership.objects.filter(
                organization=organization, user=user, is_active=True,
            ).first()
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
