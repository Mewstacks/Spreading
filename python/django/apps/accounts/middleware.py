"""Barra usuário logado com e-mail ainda não verificado.

Quem está autenticado mas não confirmou o e-mail só acessa as telas de
verificação (e logout). Qualquer outra rota redireciona p/ 'verificacao-pendente'.

Usa process_view: nesse hook o request.resolver_match (url_name) já existe — no
__call__ pré-view ele ainda é None.
"""
from django.shortcuts import redirect, render

# url_names liberados mesmo sem verificação (evita loop de redirect).
_LIBERADOS = {
    "verificacao-pendente", "reenviar-verificacao", "verificar-email",
    "logout", "login", "signup", "superadmin-parar-impersonar",
    "password_reset", "password_reset_done", "password_reset_confirm",
    "password_reset_complete",
}

# url_names sempre liberados p/ uma conta suspensa (sair / encerrar impersonação).
_LIBERADOS_BLOQUEIO = {"logout", "superadmin-parar-impersonar"}


class EmailVerificadoMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated) or user.is_superuser:
            return None
        p = request.path
        if p.startswith(("/static/", "/media/", "/admin/")):
            return None
        match = getattr(request, "resolver_match", None)
        if match and match.url_name in _LIBERADOS:
            return None
        perfil = getattr(user, "perfil", None)
        if perfil and perfil.email_verificado:
            return None
        return redirect("verificacao-pendente")


class ContaBloqueadaMiddleware:
    """Barra usuário suspenso pelo superadmin (Perfil.bloqueado).

    Conta suspensa só pode sair (logout) ou encerrar impersonação. Superadmin nunca
    é bloqueado. Resposta 403 com página explicativa (não redirect, p/ não vazar rotas).
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated) or user.is_superuser:
            return None
        if request.path.startswith(("/static/", "/media/", "/admin/")):
            return None
        match = getattr(request, "resolver_match", None)
        if match and match.url_name in _LIBERADOS_BLOQUEIO:
            return None
        perfil = getattr(user, "perfil", None)
        if perfil and perfil.bloqueado:
            return render(request, "registration/conta_suspensa.html",
                          {"motivo": perfil.bloqueado_motivo}, status=403)
        return None
