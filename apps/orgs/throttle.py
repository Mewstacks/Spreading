"""Trava de tentativas do login: senha errada demais, do mesmo endereço, fecha a porta.

Conta no banco (não na memória do processo: o web tem mais de um worker) e só no formulário
`/admin/login/`. Vale o endereço de quem conecta; atrás de um proxy com TLS (`HTTPS`), o último
item de `X-Forwarded-For`, que é o que o próprio proxy acrescentou e o cliente não consegue forjar.
Login certo zera só os erros daquele usuário naquele endereço: os chutes contra outra conta ficam.
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.http import HttpResponse
from django.utils import timezone

from .models import LoginFailure

LOGIN_PATH = "/admin/login"
PAGE = (
    '<!doctype html><html lang="pt-br"><meta charset="utf-8"><meta name="viewport" '
    'content="width=device-width,initial-scale=1"><title>Spreading</title>'
    '<body style="font-family:system-ui,sans-serif;display:grid;place-items:center;'
    'min-height:100vh;margin:0;padding:16px"><p>Muitas tentativas. Tente de novo em 15 minutos.</p>'
    "</body></html>"
)


def client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if getattr(settings, "HTTPS", False) and forwarded:
        return forwarded.split(",")[-1].strip()[:64]
    return str(request.META.get("REMOTE_ADDR", ""))[:64]


def _window():
    return timezone.now() - timedelta(minutes=settings.LOGIN_WINDOW_MINUTES)


def blocked(ip: str) -> bool:
    recent = LoginFailure.objects.filter(ip=ip, at__gte=_window()).count()
    return recent >= settings.LOGIN_MAX_FAILURES


class LoginThrottle:
    """Barra o POST do formulário de login quando o endereço já errou demais."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and request.path.startswith(LOGIN_PATH):
            if blocked(client_ip(request)):
                response = HttpResponse(PAGE, status=429)
                response["Retry-After"] = str(settings.LOGIN_WINDOW_MINUTES * 60)
                return response
        return self.get_response(request)


def _failed(sender, credentials=None, request=None, **kwargs):
    if request is None:
        return
    LoginFailure.objects.filter(at__lt=timezone.now() - timedelta(days=1)).delete()  # faxina
    LoginFailure.objects.create(
        ip=client_ip(request), username=str((credentials or {}).get("username") or "")[:150])


def _logged_in(sender, request=None, user=None, **kwargs):
    """Zera os erros deste usuário neste endereço, e só eles.

    Zerar o endereço inteiro deixava quem tem conta própria apagar os chutes contra a senha de
    outra: quatro erros no dono, um login na conta do intruso, e a contagem recomeçava do zero.
    `iexact` cobre o erro de maiúscula (o teclado do celular capitaliza o usuário sozinho).
    """
    if request is None or user is None:
        return
    LoginFailure.objects.filter(
        ip=client_ip(request), username__iexact=user.get_username()).delete()


def connect():
    user_login_failed.connect(_failed, dispatch_uid="spreading-login-failed")
    user_logged_in.connect(_logged_in, dispatch_uid="spreading-logged-in")
