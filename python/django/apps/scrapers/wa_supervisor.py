"""Vigia externo do worker WhatsApp (spreading-wa).

O watchdog interno do worker morre junto quando a VM inteira congela — e foi o que
aconteceu em 08/08: a máquina aceitava TCP em 0,03s e não devolvia uma linha de HTTP
por 20+ minutos, o SSH não entrava e o processo não logava. O Fly NÃO reinicia
máquina por health check critical (o worker é só 6PN, sem [[services]]), então sem
alguém de fora a VM fica travada para sempre. Este módulo é esse alguém: sonda o
/health do worker na cadência interna do `monitor` (POLL=15s) e, depois de falhas
seguidas demais, levanta a máquina pela Fly Machines API — /restart quando ela está
viva e travada, /start quando já caiu (a API recusa /restart em máquina parada).

Decisões:
- Sonda o /health de propósito: é a única rota sem capability auth, então a sonda
  nunca confunde problema de chave/RLS com worker morto.
- Pendurado no POLL de 15s, NÃO no tick de 5min do monitor: com 6 falhas a 15s a
  recuperação leva ~90s em vez de ~30min.
- O contador e o cooldown vivem no cache: sobrevivem a um restart do próprio
  monitor, que é o que impede um loop de restarts.
- O cooldown é armado pela TENTATIVA, não pelo sucesso: uma chamada que falha não
  pode reabrir a temporada de gestos 15s depois (ver _COOLDOWN_TENTATIVA_S).
- Sem FLY_API_TOKEN (dev) loga uma vez e vira no-op, como o snapshot do fly_infra.
"""
from __future__ import annotations

import logging
import time

import requests
from django.conf import settings
from django.core.cache import cache

from apps.scrapers import fly_infra
from apps.scrapers.eventos import log_event

logger = logging.getLogger(__name__)

_FALHAS_KEY = "wa_supervisor_falhas"
_COOLDOWN_KEY = "wa_supervisor_cooldown"
# O contador expira sozinho: se o monitor morrer no meio de uma sequência de
# falhas, a contagem velha não pode viver para sempre e assombrar o processo novo.
_FALHAS_TTL_S = 600
# Cooldown curto armado por TENTATIVA (não por sucesso). Uma tentativa que falha
# não pode voltar em 15s: em 18/08 o /restart devolveu 409 (máquina parada), a
# exceção pulou o cooldown, e o vigia passou a bater de 15 em 15s — cada chamada
# derrubava o boot do worker no meio (ele leva ~60s), deixando o WhatsApp fora do
# ar por 8h. 120s > um boot inteiro, então nunca matamos uma VM que está subindo.
_COOLDOWN_TENTATIVA_S = 120
# Estados em que a máquina JÁ está indo para 'started' sozinha: mexer nela agora
# só interrompe a transição.
_ESTADOS_EM_TRANSICAO = frozenset(
    {"created", "starting", "stopping", "replacing", "destroying"}
)
_avisou_sem_token = False


def decidir_acao(*, token: str, falhas_seguidas: int, em_cooldown: bool,
                 falhas_limite: int) -> str:
    """Decisão pura do vigia: 'noop' | 'aguardar' | 'reiniciar'.

    O restart é o gesto mais destrutivo disponível (derruba a VM no hardware),
    então ele exige o limite inteiro de falhas SEGUIDAS e respeita o cooldown —
    um boot de worker leva ~1min e sondar durante o subir seria falso positivo.
    """
    if not token:
        return "noop"
    if falhas_seguidas < falhas_limite:
        return "aguardar"
    if em_cooldown:
        return "aguardar"
    return "reiniciar"


def gesto_para_estado(estado: str) -> str:
    """'restart' | 'start' | 'aguardar' — o gesto certo p/ o estado da máquina.

    A Fly Machines API recusa /restart em máquina parada (409 Conflict), e o
    vigia antigo só conhecia o /restart: com a VM em 'stopped' ele nunca
    conseguia levantá-la. Estado desconhecido cai em 'restart', que é o gesto
    da máquina viva e o comportamento histórico.
    """
    estado = (estado or "").strip().lower()
    if estado in _ESTADOS_EM_TRANSICAO:
        return "aguardar"
    if estado in ("stopped", "suspended"):
        return "start"
    return "restart"


def _sonda_saudavel() -> bool:
    # timeout=(connect, read): com a VM travada o connect ainda completa rápido
    # (é o kernel que aceita); quem delata o travamento é o READ não responder.
    url = f"{settings.WHATSAPP_API_URL.rstrip('/')}/health"
    try:
        resp = requests.get(url, timeout=(2, 5))
        return resp.status_code == 200
    except requests.RequestException:
        return False


def _armar_cooldown(segundos: int) -> None:
    cache.set(_COOLDOWN_KEY, time.time(), timeout=segundos)
    # Zera a contagem: o boot leva ~1min e as sondas desse período não podem
    # herdar as falhas da encarnação anterior.
    cache.set(_FALHAS_KEY, 0, timeout=_FALHAS_TTL_S)


def _recuperar(falhas: int) -> str:
    """Levanta o worker de volta. Devolve o gesto tomado (p/ log)."""
    app = settings.WA_MACHINE_APP
    # O cooldown curto é armado ANTES de tocar na API: uma tentativa que falha
    # (409/412/timeout) sai por exceção e pularia o cooldown lá embaixo, e é
    # exatamente esse buraco que virou um loop de restarts de 15 em 15s.
    _armar_cooldown(_COOLDOWN_TENTATIVA_S)
    # Descobre o id pela API em cada restart: id fixo no código ficaria obsoleto
    # no primeiro replace da máquina. O app é de máquina única por desenho
    # (volume wa_data), então a primeira é a única.
    maquinas = fly_infra._listar_maquinas(app)
    if not maquinas:
        raise RuntimeError(f"nenhuma máquina encontrada no app {app}")
    alvo = maquinas[0]["id"]
    estado = str(maquinas[0].get("estado") or "")
    gesto = gesto_para_estado(estado)

    if gesto == "aguardar":
        logger.warning(
            "wa_supervisor: máquina %s (%s) em '%s'; já está subindo, sem gesto.",
            alvo, app, estado,
        )
        return "aguardar"

    if gesto == "start":
        fly_infra.iniciar_maquina(app, alvo)
        acao_humana = "ligada"
    else:
        fly_infra.reiniciar_maquina(app, alvo)
        acao_humana = "reiniciada"

    _armar_cooldown(settings.WA_SUPERVISOR_COOLDOWN_MIN * 60)
    logger.error(
        "wa_supervisor: worker sem responder em %s sondas; máquina %s (%s) %s (estado '%s').",
        falhas, alvo, app, acao_humana, estado or "?",
    )
    log_event(
        "whatsapp", "worker_reiniciado",
        f"Worker WhatsApp sem responder ao /health em {falhas} sondas seguidas; "
        f"máquina {alvo} ({app}) {acao_humana} pela API do Fly.",
        level="error",
        contexto={"app": app, "machine_id": alvo, "falhas_seguidas": falhas,
                  "estado": estado, "gesto": gesto},
    )
    return gesto


def verificar() -> str:
    """Uma passada do vigia. Nunca levanta exceção: o monitor não pode cair por
    causa de quem existe para protegê-lo. Devolve a ação tomada (p/ log)."""
    global _avisou_sem_token
    if not settings.WA_SUPERVISOR_ENABLED:
        return "desligado"
    if not settings.FLY_API_TOKEN:
        if not _avisou_sem_token:
            logger.info(
                "wa_supervisor: FLY_API_TOKEN não configurado; vigia externo em no-op (dev)."
            )
            _avisou_sem_token = True
        return "sem_token"
    try:
        if _sonda_saudavel():
            cache.set(_FALHAS_KEY, 0, timeout=_FALHAS_TTL_S)
            return "ok"
        falhas = cache.get(_FALHAS_KEY, 0) + 1
        cache.set(_FALHAS_KEY, falhas, timeout=_FALHAS_TTL_S)
        acao = decidir_acao(
            token=settings.FLY_API_TOKEN,
            falhas_seguidas=falhas,
            em_cooldown=bool(cache.get(_COOLDOWN_KEY)),
            falhas_limite=settings.WA_SUPERVISOR_FALHAS,
        )
        if acao != "reiniciar":
            logger.warning(
                "wa_supervisor: /health do worker fora do ar (%s falha(s) seguidas).", falhas
            )
            return acao
        if _recuperar(falhas) == "aguardar":
            return "aguardar"
        return "reiniciado"
    except Exception as e:  # API Fly fora, cache fora — tenta de novo na próxima
        logger.warning("wa_supervisor: passada do vigia falhou: %s", e)
        return "erro"
