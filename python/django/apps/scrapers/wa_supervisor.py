"""Vigia externo do worker WhatsApp (spreading-wa).

O watchdog interno do worker morre junto quando a VM inteira congela — e foi o que
aconteceu em 08/08: a máquina aceitava TCP em 0,03s e não devolvia uma linha de HTTP
por 20+ minutos, o SSH não entrava e o processo não logava. O Fly NÃO reinicia
máquina por health check critical (o worker é só 6PN, sem [[services]]), então sem
alguém de fora a VM fica travada para sempre. Este módulo é esse alguém: sonda o
/health do worker na cadência interna do `monitor` (POLL=15s) e, depois de falhas
seguidas demais, reinicia a máquina pela Fly Machines API.

Decisões:
- Sonda o /health de propósito: é a única rota sem capability auth, então a sonda
  nunca confunde problema de chave/RLS com worker morto.
- Pendurado no POLL de 15s, NÃO no tick de 5min do monitor: com 6 falhas a 15s a
  recuperação leva ~90s em vez de ~30min.
- O contador e o cooldown vivem no cache (Redis em produção): sobrevivem a um
  restart do próprio monitor, que é o que impede um loop de restarts.
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


def _sonda_saudavel() -> bool:
    # timeout=(connect, read): com a VM travada o connect ainda completa rápido
    # (é o kernel que aceita); quem delata o travamento é o READ não responder.
    url = f"{settings.WHATSAPP_API_URL.rstrip('/')}/health"
    try:
        resp = requests.get(url, timeout=(2, 5))
        return resp.status_code == 200
    except requests.RequestException:
        return False


def _reiniciar(falhas: int) -> None:
    app = settings.WA_MACHINE_APP
    # Descobre o id pela API em cada restart: id fixo no código ficaria obsoleto
    # no primeiro replace da máquina. O app é de máquina única por desenho
    # (volume wa_data), então a primeira é a única.
    maquinas = fly_infra._listar_maquinas(app)
    if not maquinas:
        raise RuntimeError(f"nenhuma máquina encontrada no app {app}")
    alvo = maquinas[0]["id"]
    fly_infra.reiniciar_maquina(app, alvo)
    agora = time.time()
    cache.set(_COOLDOWN_KEY, agora, timeout=settings.WA_SUPERVISOR_COOLDOWN_MIN * 60)
    # Zera a contagem: o boot leva ~1min e as sondas desse período não podem
    # herdar as falhas da encarnação anterior.
    cache.set(_FALHAS_KEY, 0, timeout=_FALHAS_TTL_S)
    logger.error(
        "wa_supervisor: worker sem responder em %s sondas; máquina %s (%s) reiniciada.",
        falhas, alvo, app,
    )
    log_event(
        "whatsapp", "worker_reiniciado",
        f"Worker WhatsApp sem responder ao /health em {falhas} sondas seguidas; "
        f"máquina {alvo} ({app}) reiniciada pela API do Fly.",
        level="error",
        contexto={"app": app, "machine_id": alvo, "falhas_seguidas": falhas},
    )


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
        _reiniciar(falhas)
        return "reiniciado"
    except Exception as e:  # API Fly fora, cache fora — tenta de novo na próxima
        logger.warning("wa_supervisor: passada do vigia falhou: %s", e)
        return "erro"
