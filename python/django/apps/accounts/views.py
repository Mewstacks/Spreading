"""Pipeline de autenticação (SaaS).

LoginRequiredMiddleware tranca o site inteiro por padrão; estas views são as
únicas públicas (marcadas com @login_not_required). Login tem rate-limit por
IP+usuário via cache para frear brute-force sem depender de Redis/axes.
"""
from django.contrib.auth import login
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters

from .forms import SignUpForm

# Trava login após N tentativas falhas numa janela curta.
LOGIN_MAX_TENTATIVAS = 8
LOGIN_JANELA_SEG = 15 * 60


def _client_ip(request):
    # Confia só no peer real (REMOTE_ADDR). X-Forwarded-For é spoofável a menos
    # que haja proxy confiável reescrevendo — não usar para decisão de segurança.
    return request.META.get("REMOTE_ADDR", "")


def _throttle_key(request):
    ip = _client_ip(request)
    user = (request.POST.get("username") or "").strip().lower()
    return f"login-fail:{ip}:{user}"


@method_decorator(never_cache, name="dispatch")
@method_decorator(login_not_required, name="dispatch")
class ThrottledLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        key = _throttle_key(request)
        if cache.get(key, 0) >= LOGIN_MAX_TENTATIVAS:
            from django.contrib import messages
            messages.error(request, "Muitas tentativas. Espere alguns minutos e tente de novo.")
            return self.render_to_response(self.get_context_data(form=self.get_form()))
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        cache.delete(_throttle_key(self.request))  # zera contador ao acertar
        return super().form_valid(form)

    def form_invalid(self, form):
        key = _throttle_key(self.request)
        cache.set(key, cache.get(key, 0) + 1, LOGIN_JANELA_SEG)
        return super().form_invalid(form)


@login_not_required
@never_cache
@csrf_protect
@sensitive_post_parameters("password1", "password2")
def signup(request):
    if request.user.is_authenticated:
        return redirect("home")
    if request.method == "POST":
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            # Dispara verificação + boas-vindas. E-mail não verificado fica barrado
            # pelo EmailVerificadoMiddleware até clicar no link.
            from .emails import enviar_verificacao, enviar_boas_vindas
            enviar_verificacao(user, request)
            enviar_boas_vindas(user)
            login(request, user)  # sessão nova já autenticada (rotaciona session key)
            return redirect("verificacao-pendente")
    else:
        form = SignUpForm()
    return render(request, "registration/signup.html", {"form": form})


@login_not_required
@never_cache
def verificar_email(request, token):
    """Link do e-mail: valida o token assinado e marca o perfil como verificado."""
    from .models import Perfil
    from .tokens import ler_token

    uid = ler_token(token)
    if uid is None:
        return render(request, "registration/verificacao_resultado.html",
                      {"ok": False}, status=400)
    perfil = Perfil.objects.filter(user_id=uid).first()
    if not perfil:
        return render(request, "registration/verificacao_resultado.html",
                      {"ok": False}, status=400)
    if not perfil.email_verificado:
        perfil.marcar_verificado()
    return render(request, "registration/verificacao_resultado.html", {"ok": True})


@never_cache
def reenviar_verificacao(request):
    """Reenvia o e-mail de verificação para o usuário logado (não verificado)."""
    user = request.user
    if user.is_authenticated and not _esta_verificado(user):
        from .emails import enviar_verificacao
        enviar_verificacao(user, request)
    return redirect("verificacao-pendente")


@never_cache
def verificacao_pendente(request):
    """Tela mostrada a quem logou mas ainda não confirmou o e-mail."""
    if request.user.is_authenticated and _esta_verificado(request.user):
        return redirect("home")
    return render(request, "registration/verificacao_pendente.html",
                  {"email": getattr(request.user, "email", "")})


def _esta_verificado(user) -> bool:
    perfil = getattr(user, "perfil", None)
    return bool(perfil and perfil.email_verificado)
