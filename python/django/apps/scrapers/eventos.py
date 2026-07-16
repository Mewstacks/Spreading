"""Operational event logging for product pipelines.

This is intentionally tiny and defensive: callers can log useful context without
turning observability into another failure point.
"""
from __future__ import annotations

import traceback


SENSITIVE_KEYS = {
    "password", "senha", "token", "secret", "api_key", "authorization",
    "credential_secret", "amazon_credential_secret", "telegram_bot_token",
}


def _clean(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_str = str(key)
            if any(s in key_str.lower() for s in SENSITIVE_KEYS):
                out[key_str] = "***"
            else:
                out[key_str] = _clean(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value[:25]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = value if not isinstance(value, str) else value[:500]
        return text
    return str(value)[:500]


def log_event(pipeline: str, evento: str, mensagem: str, *, level="info",
              usuario=None, contexto=None, exc=None):
    try:
        from apps.scrapers.models import EventoOperacional
        erro = ""
        if exc is not None:
            erro = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]
        evento_criado = EventoOperacional.objects.create(
            pipeline=pipeline,
            evento=evento[:80],
            level=level,
            mensagem=(mensagem or "")[:500],
            usuario=usuario,
            contexto=_clean(contexto or {}),
            erro=erro,
        )
        # O log cru continua imutável; o incidente é uma projeção operável dele.
        # Falhar ao atualizar a projeção nunca pode derrubar o fluxo principal.
        try:
            from apps.scrapers.incidentes_saude import processar_evento
            processar_evento(evento_criado)
            EventoOperacional.objects.filter(pk=evento_criado.pk).update(incidente_processado=True)
        except Exception:
            pass
        return evento_criado
    except Exception:
        return None
