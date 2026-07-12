"""Cabeçalhos de segurança extras que o Django não cobre nativamente.

CSP e Permissions-Policy não têm setting embutido no Django 6, então
adicionamos aqui. Mantém o resto do hardening em settings.py.
"""

import os

from django.conf import settings


# CSP permissiva o suficiente para não quebrar a UI atual:
#  - scripts/estilos inline existem em vários templates -> 'unsafe-inline'
#  - ícones lucide vêm de unpkg.com
#  - imagens de oferta vêm de CDNs externos (mercadolivre etc) -> https:
#  - QR codes podem ser data: URIs
#  - o live view do login do ML é um iframe do Browserbase -> frame-src externo
_BROWSERBASE_FRAME_SRC = "https://browserbase.com https://*.browserbase.com"
_DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    f"frame-src 'self' {_BROWSERBASE_FRAME_SRC}; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

# Permissions-Policy: desliga recursos do navegador que o app não usa.
_DEFAULT_PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), payment=(), usb=()"
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
