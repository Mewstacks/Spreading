"""Cabeçalhos de segurança extras que o Django não cobre nativamente.

CSP e Permissions-Policy não têm setting embutido no Django 6, então
adicionamos aqui. Mantém o resto do hardening em settings.py.
"""

from django.conf import settings


# CSP permissiva o suficiente para não quebrar a UI atual:
#  - scripts/estilos inline existem em vários templates -> 'unsafe-inline'
#  - ícones lucide vêm de unpkg.com
#  - imagens de oferta vêm de CDNs externos (mercadolivre etc) -> https:
#  - QR codes podem ser data: URIs
_DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

# Permissions-Policy: desliga recursos do navegador que o app não usa.
_DEFAULT_PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), payment=(), usb=()"
)


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
