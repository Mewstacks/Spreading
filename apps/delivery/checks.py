"""Checagem agendada de cada número de WhatsApp.

A cada ciclo, uma sonda: a sessão está em WORKING e o WhatsApp responde pelo próprio número, sem
mandar mensagem. Com um chat de canário configurado, a cada 2 a 4 horas uma mensagem real e a
conferência do ACK ("conectado" não prova entrega). O resultado vira `SourceRun` (`wa:<número>` e
`wc:<número>`), que alimenta a tela WhatsApp, o /status e os alertas.
"""
import hashlib
import time
from datetime import timedelta

from django.conf import settings

from apps.offers.models import SourceRun
from apps.offers.source_state import record

from .health import canary_chats, run_canary, run_probe

CANARY_EVERY = timedelta(hours=2)
CANARY_SPREAD = timedelta(hours=2)  # o intervalo do canário vai de 2 a 4 h


def probe_label(name: str) -> str:
    return f"wa:{name}"[:40]


def canary_label(name: str) -> str:
    return f"wc:{name}"[:40]


def sessions_to_check(transport) -> list[str]:
    """O número principal e os que têm regra ativa ou item na fila."""
    names = {settings.WAHA_SESSION}
    if hasattr(transport, "used_sessions"):
        names |= transport.used_sessions()
    return sorted(names)


def canary_due(name: str, now) -> bool:
    """O canário vence de 2 a 4 h depois do anterior; o sorteio é fixo por número e tentativa
    anterior, então o mesmo ciclo não "tenta a sorte" de 5 em 5 minutos."""
    last = (SourceRun.objects.filter(label=canary_label(name))
            .values_list("checked_at", flat=True).first())
    if last is None:
        return True
    digest = hashlib.sha256(f"{name}:{last.isoformat()}".encode()).digest()
    fraction = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    return now - last >= CANARY_EVERY + CANARY_SPREAD * fraction


def run(transport, now, sleep=time.sleep) -> dict:
    """Sonda cada número e, se há chat e venceu o prazo, manda o canário. {número: resultado}."""
    chats = canary_chats()
    out = {}
    for name in sessions_to_check(transport):
        sender = transport.for_session(name) if hasattr(transport, "for_session") else transport
        probe = run_probe(sender)
        record(probe_label(name), now, error=None if probe["ok"] else probe["reason"],
               value="ok (sondagem)" if probe["ok"] else None)
        status = "ok" if probe["ok"] else "falhou"
        chat = chats.get(name, "")
        if probe["ok"] and chat and canary_due(name, now):
            result = run_canary(sender, chat, sleep=sleep)
            record(canary_label(name), now, error=None if result["ok"] else result["reason"],
                   value=f"ack {result['ack']}" if result["ok"] else None)
            status += "+canario" + ("" if result["ok"] else " falhou")
        out[name] = status
    return out
