"""Workspace do superadmin (is_superuser).

Vê todos os usuários, uso computado (envios/configs/conexões), saúde das máquinas
Fly (compartilhadas) e permite suspender, definir cotas e impersonar um usuário.

Uso é COMPUTADO das tabelas existentes — não há metering novo. Infra é compartilhada
(uma máquina por serviço), então o painel Fly é global, não por usuário.
"""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.db.models import Count, Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.scrapers import automacao_state as st
from apps.scrapers.fly_infra import snapshot as fly_snapshot
from apps.scrapers.models import (
    CanalMonitorado, ConfiguracaoEnvio, HistoricoEnvio,
)
from apps.scrapers.views import superadmin_required

User = get_user_model()

# Chave da sessão que guarda o superadmin original durante a impersonação.
IMPERSONATOR_KEY = "impersonator_id"


def _uso_usuario(user, *, agora=None) -> dict:
    """Uso computado de um usuário a partir das tabelas existentes (sem metering novo)."""
    agora = agora or timezone.now()
    perfil = getattr(user, "perfil", None)
    corte7 = agora - timedelta(days=7)
    hoje = timezone.localtime(agora).date()

    envios_qs = HistoricoEnvio.objects.filter(usuario=user)
    envios_hoje = envios_qs.filter(data_envio__date=hoje).count()
    return {
        "user": user,
        "perfil": perfil,
        "verificado": bool(perfil and perfil.email_verificado),
        "bloqueado": bool(perfil and perfil.bloqueado),
        "wa_estado": getattr(perfil, "wa_estado", None),
        "ml_estado": getattr(perfil, "ml_estado", None),
        "amazon_elegivel": getattr(perfil, "amazon_elegivel", None),
        "amazon_erro": getattr(perfil, "amazon_ultimo_erro", ""),
        "configs_ativas": ConfiguracaoEnvio.objects.filter(owner=user, ativo=True).count(),
        "canais": CanalMonitorado.objects.filter(owner=user).count(),
        "envios_total": envios_qs.count(),
        "envios_7d": envios_qs.filter(data_envio__gte=corte7).count(),
        "envios_hoje": envios_hoje,
        "ultima_atividade": envios_qs.aggregate(m=Max("data_envio"))["m"],
        # Cotas (com fallback global) p/ exibição/edição.
        "cota_configs": perfil.cota_max_configs() if perfil else 0,
        "cota_envios_dia": perfil.cota_max_envios_dia() if perfil else 0,
        "cota_wa": perfil.cota_max_wa_sessions() if perfil else 0,
    }


@superadmin_required
def superadmin_usuarios(request):
    """Tabela de todos os usuários + uso computado + saúde das conexões."""
    users = (User.objects.select_related("perfil")
             .order_by("-is_superuser", "-date_joined"))
    linhas = [_uso_usuario(u) for u in users]
    ctx = {
        "linhas": linhas,
        "total_users": len(linhas),
        # Loops são globais (single-tenant em transição): heartbeat mostrado uma vez.
        "worker_scrape": st.worker_alive("scrape"),
        "worker_envio": st.worker_alive("envio"),
        "scrape_ligado": st.is_enabled("scrape"),
        "envio_ligado": st.is_enabled("envio"),
    }
    return render(request, "scrapers/superadmin/usuarios.html", ctx)


@superadmin_required
def superadmin_usuario_detalhe(request, user_id):
    user = get_object_or_404(User.objects.select_related("perfil"), pk=user_id)
    return render(request, "scrapers/superadmin/usuario_detalhe.html",
                  {"u": _uso_usuario(user)})


@superadmin_required
def superadmin_infra(request):
    """Painel global das máquinas Fly (compartilhadas). Somente leitura."""
    snap = fly_snapshot(force=request.GET.get("force") == "1")
    # Sessões WhatsApp ativas ~ usuários com wa_estado True (cap da máquina: ~3-4/2GB).
    wa_ativas = User.objects.filter(perfil__wa_estado=True).count()
    return render(request, "scrapers/superadmin/infra.html",
                  {"fly": snap, "wa_ativas": wa_ativas})


@superadmin_required
@require_POST
def superadmin_suspender(request, user_id):
    user = get_object_or_404(User, pk=user_id)
    if user.is_superuser:
        messages.error(request, "Não é possível suspender um superadmin.")
        return redirect("superadmin-usuario", user_id=user_id)
    perfil = user.perfil
    if perfil.bloqueado:
        perfil.desbloquear()
        messages.success(request, f"{user.get_username()} reativado.")
    else:
        motivo = request.POST.get("motivo", "").strip()
        perfil.marcar_bloqueado(motivo)
        messages.success(request, f"{user.get_username()} suspenso.")
    return redirect("superadmin-usuario", user_id=user_id)


@superadmin_required
@require_POST
def superadmin_cotas(request, user_id):
    user = get_object_or_404(User, pk=user_id)
    perfil = user.perfil

    def _int(nome):
        try:
            return max(0, int(request.POST.get(nome, "0") or 0))
        except ValueError:
            return 0

    perfil.max_configs = _int("max_configs")
    perfil.max_envios_dia = _int("max_envios_dia")
    perfil.max_wa_sessions = _int("max_wa_sessions")
    perfil.save(update_fields=["max_configs", "max_envios_dia", "max_wa_sessions"])
    messages.success(request, "Cotas atualizadas (0 = usa o default global).")
    return redirect("superadmin-usuario", user_id=user_id)


# ── Impersonação (session-swap, sem dependência externa) ──────────────
@superadmin_required
@require_POST
def superadmin_impersonar(request, user_id):
    alvo = get_object_or_404(User, pk=user_id)
    if alvo.id == request.user.id:
        return redirect("superadmin-usuario", user_id=user_id)
    real_id = request.user.id
    login(request, alvo)               # cicla a sessão…
    request.session[IMPERSONATOR_KEY] = real_id   # …então grava o superadmin original
    messages.info(request, f"Você está agora como {alvo.get_username()}.")
    return redirect("home")


@require_POST
def superadmin_parar_impersonar(request):
    """Volta ao superadmin original. NÃO exige is_superuser — durante a impersonação
    request.user é o alvo. Autorizado apenas pela presença da chave de sessão."""
    imp_id = request.session.get(IMPERSONATOR_KEY)
    if not imp_id:
        return redirect("home")
    try:
        admin = User.objects.get(pk=imp_id, is_superuser=True)
    except User.DoesNotExist:
        request.session.pop(IMPERSONATOR_KEY, None)
        return redirect("home")
    login(request, admin)              # cicla a sessão e limpa a chave
    messages.success(request, "Impersonação encerrada.")
    return redirect("superadmin-usuarios")
