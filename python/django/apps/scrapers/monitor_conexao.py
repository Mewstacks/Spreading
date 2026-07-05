"""Watchdog de conexões — avisa o usuário por e-mail quando WhatsApp ou ML cai.

Roda a cada tick do loop de envio (e via `manage.py monitorar`). Compara o estado
atual de cada conexão com o último estado salvo no Perfil; em transição manda e-mail
(caiu / reconectou), com cooldown p/ não floodar enquanto seguir caído.

Hoje WhatsApp/ML são globais (single-tenant em transição). As funções já recebem o
usuário p/ quando a Fase 3 isolar conexão por usuário (sessão WA + auth_{id}.json).
"""
import os

from django.conf import settings
from django.utils import timezone


def ml_auth_path(user=None) -> str:
    """Caminho do auth.json do ML. Delega ao resolvedor único (honra ML_AUTH_DIR)."""
    from apps.scrapers.session_paths import ml_auth_path as _resolver
    return _resolver(user)


def ml_conectado(user=None) -> bool:
    """ML 'conectado' = auth.json existe e foi atualizado dentro de ML_AUTH_STALE_DIAS."""
    path = ml_auth_path(user)
    if not os.path.exists(path):
        return False
    idade_dias = (timezone.now().timestamp() - os.path.getmtime(path)) / 86400.0
    return idade_dias <= getattr(settings, "ML_AUTH_STALE_DIAS", 7)


def wa_conectado(session=None) -> bool:
    from apps.scrapers import whatsapp_client
    try:
        return bool(whatsapp_client.status().get("conectado"))
    except Exception:
        return False


def verificar_e_notificar() -> dict:
    """Checa todos os perfis verificados e dispara alertas. Retorna contadores."""
    from datetime import timedelta
    from apps.accounts.models import Perfil
    from apps.accounts.emails import enviar_alerta_conexao

    agora = timezone.now()
    cooldown = timedelta(hours=getattr(settings, "ALERTA_CONEXAO_COOLDOWN_H", 6))
    enviados = 0
    checados = 0

    wa_global = wa_conectado()  # Fase 3: por sessão de cada usuário.

    perfis = (Perfil.objects.select_related("user")
              .filter(user__is_active=True, email_verificado=True)
              .exclude(user__email=""))
    for perfil in perfis:
        checados += 1
        wa = wa_global
        ml = ml_conectado(perfil.user)
        enviados += _processar(perfil, "WhatsApp", "wa", wa, agora, cooldown,
                               enviar_alerta_conexao)
        enviados += _processar(perfil, "Mercado Livre", "ml", ml, agora, cooldown,
                               enviar_alerta_conexao)
    return {"checados": checados, "alertas_enviados": enviados}


def _processar(perfil, nome_servico, campo, conectado, agora, cooldown, enviar) -> int:
    """Compara estado atual vs salvo; manda e-mail em transição (com cooldown). 1 se enviou."""
    estado_attr = f"{campo}_estado"
    alerta_attr = f"alerta_{campo}_em"
    anterior = getattr(perfil, estado_attr)        # True | False | None (nunca checado)
    ultimo_alerta = getattr(perfil, alerta_attr)
    enviou = 0

    if not conectado:
        primeira_vez = anterior is not False        # True ou None -> acabou de cair
        cooldown_ok = ultimo_alerta is None or (agora - ultimo_alerta) >= cooldown
        if primeira_vez or cooldown_ok:
            if enviar(perfil.user, nome_servico, caiu=True):
                setattr(perfil, alerta_attr, agora)
                enviou = 1
    else:
        if anterior is False:                       # estava caído -> reconectou
            if enviar(perfil.user, nome_servico, caiu=False):
                enviou = 1

    setattr(perfil, estado_attr, conectado)
    perfil.save(update_fields=[estado_attr, alerta_attr])
    return enviou
