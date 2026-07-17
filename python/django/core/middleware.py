"""Cabeçalhos de segurança extras que o Django não cobre nativamente.

CSP e Permissions-Policy não têm setting embutido no Django 6, então
adicionamos aqui. Mantém o resto do hardening em settings.py.
"""

import os
import logging

from django.conf import settings
from django.db import DatabaseError, InterfaceError, connections
from django.http import HttpResponse


logger = logging.getLogger("core.database")


# CSP permissiva o suficiente para não quebrar a UI atual:
#  - scripts/estilos inline existem em vários templates -> 'unsafe-inline'
#  - ícones lucide vêm de unpkg.com
#  - imagens de oferta vêm de CDNs externos (mercadolivre etc) -> https:
#  - QR codes e os frames do live view (login ML) são data: URIs -> img-src data:
#  - o live view do login do ML é um <canvas> alimentado por SSE (mesma origem):
#    frames por EventSource + input por fetch -> connect-src 'self' já cobre.
_DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

# Permissions-Policy: desliga recursos do navegador que o app não usa.
_DEFAULT_PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), payment=(), usb=()"
)


class DatabaseUnavailableMiddleware:
    """Converte falhas transitórias de Postgres em 503 controlado.

    Fica antes de SessionMiddleware para também proteger a leitura da sessão feita
    pelo AuthenticationMiddleware. Não tenta registrar nada no banco enquanto ele
    está indisponível e fecha os sockets ruins antes da próxima requisição.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Curto-circuita antes de Session/Auth e de qualquer middleware que leia
        # Perfil. O check continua sendo de prontidão real: SELECT 1 no banco.
        if request.path == "/healthz":
            health_connection = connections["default"]
            try:
                # Não reutiliza uma conexão persistente que possa ter morrido no
                # proxy. `connect_timeout=3` em settings limita a tentativa nova.
                health_connection.close()
                with health_connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                return HttpResponse("ok", content_type="text/plain")
            except (DatabaseError, InterfaceError):
                health_connection.close()
                return HttpResponse("database unavailable", status=503,
                                    content_type="text/plain")
            finally:
                health_connection.close()
        try:
            return self.get_response(request)
        except (DatabaseError, InterfaceError) as exc:
            connections.close_all()
            logger.warning("Banco indisponível em %s: %s", request.path, exc)
            return HttpResponse(
                "Serviço temporariamente indisponível. Tente novamente em instantes.",
                status=503,
                content_type="text/plain; charset=utf-8",
                headers={"Retry-After": "15", "Cache-Control": "no-store"},
            )


class DevAutoLoginMiddleware:
    """DEV: dispensa o login local. Só ativa com DEBUG e DEV_AUTOLOGIN != '0'.

    Quando a requisição chega anônima, anexa um superusuário de desenvolvimento
    ao request.user (em memória, sem gravar sessão). Assim o LoginRequiredMiddleware
    deixa passar e as views que usam request.user.perfil continuam funcionando.
    NUNCA roda em produção (guardado por settings.DEBUG).
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = settings.DEBUG and os.getenv("DEV_AUTOLOGIN", "1") != "0"
        self._user = None

    def _dev_user(self):
        # get_or_create tardio (o banco pode não existir ainda no import).
        if self._user is not None:
            return self._user
        from django.contrib.auth import get_user_model
        User = get_user_model()
        username = os.getenv("DEV_AUTOLOGIN_USER", "dev")
        user, criado = User.objects.get_or_create(
            username=username,
            defaults={"email": f"{username}@localhost", "is_staff": True,
                      "is_superuser": True},
        )
        if criado:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        self._user = user
        return user

    def __call__(self, request):
        if self.enabled:
            user = getattr(request, "user", None)
            if user is None or not user.is_authenticated:
                try:
                    request.user = self._dev_user()
                except Exception:
                    pass  # banco indisponível (ex: migrate) — segue anônimo
        return self.get_response(request)


class SecurityHeadersMiddleware:
    """Adiciona Content-Security-Policy e Permissions-Policy a cada resposta."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.csp = getattr(settings, "CONTENT_SECURITY_POLICY", _DEFAULT_CSP)
        self.csp_report_only = getattr(settings, "CSP_REPORT_ONLY", False)
        self.permissions_policy = getattr(
            settings, "PERMISSIONS_POLICY", _DEFAULT_PERMISSIONS_POLICY
        )

    def __call__(self, request):
        response = self.get_response(request)
        header = (
            "Content-Security-Policy-Report-Only"
            if self.csp_report_only
            else "Content-Security-Policy"
        )
        response.setdefault(header, self.csp)
        response.setdefault("Permissions-Policy", self.permissions_policy)
        return response
