"""Watchdog de conexões — avisa quando WhatsApp ou ML cai.

Roda no processo `monitor` do Procfile (`manage.py monitorar`). Compara o estado
atual de cada conexão com o último estado salvo no Perfil; em transição registra um
EventoOperacional (pipeline "conexao", visível em /painel-admin/saude) e manda e-mail
ao usuário, com cooldown p/ não floodar enquanto seguir caído.

O estado atual vem de `conexoes.py` — a fonte única que as telas também leem. Este
módulo não decide mais o que é "conectado"; ele só detecta TRANSIÇÃO e reage. Era
justamente por decidir por conta própria que ele divergia do dashboard.

O evento e o e-mail são independentes de propósito: o e-mail depende de SMTP
configurado e é para o usuário; o evento é nosso e precisa existir mesmo quando o
e-mail não sai — foi assim que quedas passaram meses invisíveis.

Hoje WhatsApp/ML são globais (single-tenant em transição). As funções já recebem o
usuário p/ quando a Fase 3 isolar conexão por usuário (sessão WA + auth_{id}.json).
"""
from django.utils import timezone


def ml_auth_path(user=None) -> str:
    """Caminho do auth.json do ML. Delega ao resolvedor único (honra ML_AUTH_DIR)."""
    from apps.scrapers.session_paths import ml_auth_path as _resolver
    return _resolver(user)


def ml_conectado(user=None) -> bool:
    """True se o ML ainda aceita a sessão salva. Wrapper sobre conexoes.estado_ml.

    Mantido pela assinatura: relatorios.py e automacao.py já dependiam dele. A
    regra real (sonda de sessão, antes era a idade do arquivo) mora em conexoes.py.
    """
    from apps.scrapers.conexoes import estado_ml
    return estado_ml(user).conectado


def wa_conectado(session=None) -> bool:
    """True se o worker reporta o WhatsApp pareado. Wrapper sobre conexoes.estado_whatsapp."""
    from apps.scrapers.conexoes import estado_whatsapp
    return estado_whatsapp(session=session).conectado


def verificar_e_notificar() -> dict:
    """Checa todos os perfis verificados e dispara alertas. Retorna contadores."""
    from datetime import timedelta
    from django.conf import settings
    from apps.accounts.models import Perfil
    from apps.accounts.emails import enviar_alerta_conexao
    from apps.scrapers.conexoes import estado_amazon_relatorios, estado_ml, estado_whatsapp

    agora = timezone.now()
    cooldown = timedelta(hours=getattr(settings, "ALERTA_CONEXAO_COOLDOWN_H", 6))
    enviados = 0
    checados = 0

    perfis = (Perfil.objects.select_related("user")
              .filter(user__is_active=True, email_verificado=True)
              .exclude(user__email=""))
    for perfil in perfis:
        checados += 1
        # Estado rico (não bool): o motivo entra no evento, e é ele que a Saúde
        # mostra. "WhatsApp caiu" sem dizer se foi o pareamento ou o serviço fora
        # do ar não é acionável.
        wa = estado_whatsapp(perfil.user, session=perfil.sessao_whatsapp())
        ml = estado_ml(perfil.user)
        amazon = estado_amazon_relatorios(perfil.user)
        enviados += _processar(perfil, "WhatsApp", "wa", wa, agora, cooldown,
                               enviar_alerta_conexao)
        enviados += _processar(perfil, "Mercado Livre", "ml", ml, agora, cooldown,
                               enviar_alerta_conexao)
        # Só alertamos quem já usa Amazon: uma conta sem tag não pediu integração.
        if perfil.amazon_conectado():
            enviados += _processar(perfil, "Amazon Relatórios", "amazon_relatorio", amazon,
                                   agora, cooldown, enviar_alerta_conexao)
    return {"checados": checados, "alertas_enviados": enviados}


def _processar(perfil, nome_servico, campo, estado, agora, cooldown, enviar) -> int:
    """Compara estado atual vs salvo; alerta em transição (com cooldown). 1 se enviou e-mail."""
    from apps.scrapers.eventos import log_event

    estado_attr = f"{campo}_estado"
    alerta_attr = f"alerta_{campo}_em"
    anterior = getattr(perfil, estado_attr)        # True | False | None (nunca checado)
    ultimo_alerta = getattr(perfil, alerta_attr)
    conectado = estado.conectado
    enviou = 0

    if not conectado:
        primeira_vez = anterior is not False        # True ou None -> acabou de cair
        cooldown_ok = ultimo_alerta is None or (agora - ultimo_alerta) >= cooldown
        if primeira_vez or cooldown_ok:
            # O carimbo marca a TENTATIVA, não o sucesso do e-mail. Antes só era gravado
            # quando o envio dava certo, e com SMTP quebrado ele ficava None para sempre:
            # o cooldown nunca fechava e o alerta era retentado a cada tick (5min). Isso
            # passava despercebido porque ninguém contava e-mail que não sai — mas agora
            # cada tentativa gera evento, e o relatório afogaria em 288 linhas/dia por
            # usuário caído. Retentar SMTP quebrado de 5 em 5 min também nunca ajudou.
            setattr(perfil, alerta_attr, agora)
            # Evento independente do e-mail: o alerta depende de SMTP configurado, o
            # relatório não pode depender. Cai no mesmo cooldown, então uma conexão
            # cronicamente fora gera ~4 eventos/dia, não 288.
            log_event(
                "conexao", "conexao_caiu",
                f"{nome_servico} de {perfil.user.get_username()} está fora do ar: "
                f"{estado.motivo or 'motivo não informado'}",
                level="error", usuario=perfil.user,
                contexto={"servico": nome_servico, "repique": not primeira_vez,
                          "motivo": estado.motivo, "detalhe": estado.detalhe,
                          "fonte": estado.fonte},
            )
            if enviar(perfil.user, nome_servico, caiu=True):
                enviou = 1
    else:
        if anterior is False:                       # estava caído -> reconectou
            log_event(
                "conexao", "conexao_voltou",
                f"{nome_servico} de {perfil.user.get_username()} reconectou.",
                usuario=perfil.user, contexto={"servico": nome_servico},
            )
            if enviar(perfil.user, nome_servico, caiu=False):
                enviou = 1

    setattr(perfil, estado_attr, conectado)
    perfil.save(update_fields=[estado_attr, alerta_attr])
    return enviou
