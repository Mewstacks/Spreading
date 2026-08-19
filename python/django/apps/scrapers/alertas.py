"""Leva um incidente até uma PESSOA, em minutos.

O sistema já detecta muito bem: `EventoOperacional` guarda o log, `IncidenteSaude` o
projeta deduplicado, o Sentry recebe todo `ERROR` e ainda dispara um autofix no
GitHub. Só faltava a parte que decide se alguém fica sabendo — e sem ela o modo de
falha real é o que está escrito no próprio workflow de energia da produção:

    "deixa a produção fora do ar até alguém perceber, que foi o que aconteceu em
     16, 17 e 18/08."

Uma tela de Saúde vermelha só avisa quem já abriu a tela. Quem opera este produto são
influenciadores: quando o envio para, ou uma oferta ruim escapa, a diferença entre
cinco minutos e um dia é a reputação deles.

Três decisões de projeto, todas para que o alerta não vire o próximo problema:

* **Avisa na ABERTURA, não em toda ocorrência.** `IncidenteSaude` já é deduplicado
  por `chave`; um incidente que se repete 400 vezes é um alerta, não 400. Reabertura
  depois de resolvido também avisa — é informação nova.
* **Silêncio por chave.** Mesmo abrindo e fechando em sequência, cada chave só fala
  uma vez dentro de `ALERTA_SILENCIO_MIN`. Alerta que toca demais é alerta que se
  aprende a ignorar, e aí ele custa mais do que não ter.
* **Nunca levanta exceção.** Isto roda dentro do caminho de log de erro. Um alerta que
  quebra derruba justamente o fluxo que estava tentando reportar um problema.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Só estes níveis acordam alguém. `info` e `warning` vivem na tela de Saúde.
NIVEIS_QUE_ALERTAM = {"error"}

_TIMEOUT = 8
_PREFIXO_SILENCIO = "alerta-incidente:"


def _silencio_minutos() -> int:
    return max(1, int(getattr(settings, "ALERTA_SILENCIO_MIN", 60) or 60))


def _destinos():
    """(chat_id do Telegram, lista de e-mails). Vazio = canal desligado."""
    chat = str(getattr(settings, "ALERTA_TELEGRAM_CHAT_ID", "") or "").strip()
    emails = [
        e.strip() for e in
        str(getattr(settings, "ALERTA_EMAILS", "") or "").split(",")
        if e.strip()
    ]
    return chat, emails


def _texto(incidente) -> str:
    escopo = str(getattr(incidente, "escopo", "") or "sistema")
    causa = str(getattr(incidente, "causa", "") or "?")
    pipeline = str(getattr(incidente, "pipeline", "") or "?")
    mensagem = str(getattr(incidente, "ultima_mensagem", "") or "").strip()
    return (
        f"[Spreading] incidente aberto\n"
        f"causa: {causa}\n"
        f"pipeline: {pipeline}\n"
        f"escopo: {escopo}\n"
        f"{mensagem[:400]}"
    )


def _enviar_telegram(chat_id: str, texto: str) -> bool:
    token = str(getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
    if not token or not chat_id:
        return False
    dados = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": texto,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    pedido = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=dados,
    )
    with urllib.request.urlopen(pedido, timeout=_TIMEOUT) as resposta:
        corpo = json.loads(resposta.read().decode("utf-8", "ignore") or "{}")
    return bool(corpo.get("ok"))


def _enviar_email(emails, texto: str) -> bool:
    from django.core.mail import send_mail

    if not emails:
        return False
    send_mail(
        subject="[Spreading] incidente aberto em produção",
        message=texto,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        recipient_list=emails,
        fail_silently=False,
    )
    return True


def deve_alertar(incidente, *, criado: bool, reaberto: bool) -> bool:
    """Só abertura e reabertura de incidente `error`, uma vez por janela de silêncio."""
    if not (criado or reaberto):
        return False
    if str(getattr(incidente, "level", "")) not in NIVEIS_QUE_ALERTAM:
        return False
    chave = f"{_PREFIXO_SILENCIO}{getattr(incidente, 'chave', '') or incidente.pk}"
    # `cache.add` é atômico: dois workers projetando o mesmo evento no mesmo
    # instante não mandam dois alertas.
    return bool(cache.add(chave, "1", timeout=_silencio_minutos() * 60))


def notificar_incidente(incidente, *, criado=False, reaberto=False) -> bool:
    """Manda o incidente para o canal de operação. Devolve se alguém foi avisado.

    Nunca levanta: qualquer falha aqui vira log e nada mais (ver docstring do módulo).
    """
    try:
        if not deve_alertar(incidente, criado=criado, reaberto=reaberto):
            return False
        chat_id, emails = _destinos()
        if not chat_id and not emails:
            # Canal não configurado é estado normal em desenvolvimento; dizer isso a
            # cada incidente encheria o log de produção quando alguém esquecer de
            # configurar — então fica em DEBUG e a tela de Saúde continua sendo a fonte.
            logger.debug("Nenhum canal de alerta configurado; incidente só na Saúde.")
            return False
        texto = _texto(incidente)
        entregue = False
        try:
            entregue = _enviar_telegram(chat_id, texto) or entregue
        except Exception as exc:
            logger.warning("Alerta por Telegram falhou (%s).", type(exc).__name__)
        try:
            entregue = _enviar_email(emails, texto) or entregue
        except Exception as exc:
            logger.warning("Alerta por e-mail falhou (%s).", type(exc).__name__)
        if not entregue:
            # Nenhum canal entregou: solta o silêncio para que a próxima ocorrência
            # tente de novo. Sem isso, uma falha momentânea do Telegram calaria o
            # incidente pela janela inteira.
            cache.delete(f"{_PREFIXO_SILENCIO}{getattr(incidente, 'chave', '')}")
        return entregue
    except Exception:
        logger.exception("Falha inesperada ao notificar incidente")
        return False
