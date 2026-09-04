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

Quatro decisões de projeto, todas para que o alerta não vire o próximo problema:

* **Avisa na ABERTURA, não em toda ocorrência.** `IncidenteSaude` já é deduplicado
  por `chave`; um incidente que se repete 400 vezes é um alerta, não 400. Reabertura
  depois de resolvido também avisa — é informação nova.
* **Silêncio por incidente, no banco — não no cache.** Produção roda LocMemCache
  (sem Redis) com dez processos por VM: dedupe em cache vale por processo, então o
  mesmo incidente mandava até dez mensagens. A janela de silêncio mora em duas
  colunas de `IncidenteSaude` (`alertado_em`/`alerta_tentado_em`) e a reivindicação
  é um UPDATE condicional — o lock de linha do PostgreSQL serializa processos e VMs,
  não só threads de um processo.
* **Nunca levanta exceção.** Isto roda dentro do caminho de log de erro. Um alerta que
  quebra derruba justamente o fluxo que estava tentando reportar um problema.
* **Sempre em contexto de sistema.** `notificar_incidente` é alcançado a partir de
  `log_event`, que roda em qualquer contexto de tenant. `scrapers_incidentesaude` é
  tabela mixed (RLS): a reivindicação fora de `system_context()` casaria 0 linhas em
  contexto de organização e o alerta sumiria em silêncio — o mesmo modo de falha que
  a migração 0064 teve.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# Só estes níveis acordam alguém. `info` e `warning` vivem na tela de Saúde.
NIVEIS_QUE_ALERTAM = {"error"}

_TIMEOUT = 8
# Teto para uma reivindicação órfã (processo morreu entre reivindicar e
# entregar) devolver o direito de tentar sozinha, sem depender do caminho de
# falha explícito liberar `alerta_tentado_em`.
_TENTATIVA_TTL_MIN = 5


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


def _reivindicar(incidente) -> bool:
    """UPDATE condicional: ganha quem casar a linha primeiro.

    Sob READ COMMITTED, o segundo UPDATE concorrente reavalia o predicado
    depois do commit do primeiro — vê `alerta_tentado_em` recém-gravado e casa
    0 linhas. É essa propriedade do banco que substitui o `cache.add` atômico
    e passa a valer entre processos e entre VMs, não só dentro de um processo.
    """
    from apps.scrapers.models import IncidenteSaude

    agora = timezone.now()
    limite_silencio = agora - timedelta(minutes=_silencio_minutos())
    limite_tentativa = agora - timedelta(minutes=_TENTATIVA_TTL_MIN)
    try:
        ganhou = IncidenteSaude.objects.filter(pk=incidente.pk).filter(
            Q(alertado_em__isnull=True) | Q(alertado_em__lt=limite_silencio),
        ).filter(
            Q(alerta_tentado_em__isnull=True) | Q(alerta_tentado_em__lt=limite_tentativa),
        ).update(alerta_tentado_em=agora)
    except DatabaseError as exc:
        # Sem banco não há dedup possível — e alertar sem dedup é exatamente a
        # tempestade que este mecanismo existe para evitar. Fail-closed:
        # melhor um incidente silencioso (a tela de Saúde e o Sentry cobrem)
        # do que dez mensagens repetidas.
        logger.warning("Reivindicação de alerta indisponível (banco): %s", exc)
        return False
    return bool(ganhou)


def _confirmar_entrega(incidente):
    from apps.scrapers.models import IncidenteSaude
    IncidenteSaude.objects.filter(pk=incidente.pk).update(alertado_em=timezone.now())


def _liberar_tentativa(incidente):
    """Devolve o direito de tentar depois de uma entrega que falhou.

    Sem isto, uma falha momentânea do Telegram/SMTP calaria o incidente pela
    janela de silêncio inteira — o mesmo bug que o `cache.delete` original
    tentava cobrir, mas sem o descuido de usar a chave errada quando `chave`
    está vazia (o `cache.delete` antigo perdia o fallback `or incidente.pk`
    que o `add` tinha).
    """
    from apps.scrapers.models import IncidenteSaude
    try:
        IncidenteSaude.objects.filter(pk=incidente.pk).update(alerta_tentado_em=None)
    except DatabaseError as exc:
        logger.warning("Falha ao liberar reivindicação de alerta: %s", exc)


def deve_alertar(incidente, *, criado: bool, reaberto: bool) -> bool:
    """Só abertura e reabertura de incidente `error`, uma vez por janela de silêncio."""
    if not (criado or reaberto):
        return False
    if str(getattr(incidente, "level", "")) not in NIVEIS_QUE_ALERTAM:
        return False
    return _reivindicar(incidente)


def notificar_incidente(incidente, *, criado=False, reaberto=False) -> bool:
    """Manda o incidente para o canal de operação. Devolve se alguém foi avisado.

    Nunca levanta: qualquer falha aqui vira log e nada mais (ver docstring do
    módulo). Roda sempre em contexto de sistema: log_event alcança este
    caminho a partir de qualquer contexto de tenant, e a tabela é mixed —
    fora de system_context() a reivindicação casaria 0 linhas em silêncio.
    """
    from apps.accounts.tenant import system_context

    try:
        with system_context():
            if not deve_alertar(incidente, criado=criado, reaberto=reaberto):
                return False
            chat_id, emails = _destinos()
            if not chat_id and not emails:
                # Canal não configurado é estado normal em desenvolvimento; dizer isso
                # a cada incidente encheria o log de produção quando alguém esquecer
                # de configurar — então fica em DEBUG e a Saúde é a fonte. Libera a
                # reivindicação: sem isso a linha ficaria presa por
                # _TENTATIVA_TTL_MIN sem nenhuma tentativa real ter ocorrido.
                logger.debug("Nenhum canal de alerta configurado; incidente só na Saúde.")
                _liberar_tentativa(incidente)
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
            if entregue:
                _confirmar_entrega(incidente)
            else:
                _liberar_tentativa(incidente)
            return entregue
    except Exception:
        logger.exception("Falha inesperada ao notificar incidente")
        return False
