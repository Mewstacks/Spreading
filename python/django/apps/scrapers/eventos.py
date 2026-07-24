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

# Mapeia o level do log_event para a severidade do Sentry.
_SENTRY_LEVEL = {
    "debug": "debug", "info": "info", "warning": "warning",
    "error": "error", "critical": "fatal",
}


def _report_sentry(exc, *, pipeline, evento, level, usuario, contexto):
    """Envia a exceção ao Sentry se disponível. Nunca derruba o fluxo principal."""
    try:
        import sentry_sdk
    except Exception:
        return
    try:
        with sentry_sdk.push_scope() as scope:
            scope.set_level(_SENTRY_LEVEL.get(level, "error"))
            scope.set_tag("pipeline", pipeline)
            scope.set_tag("evento", (evento or "")[:80])
            scope.set_context("evento_operacional", _clean(contexto or {}))
            if usuario is not None:
                scope.set_user({
                    "id": getattr(usuario, "pk", None),
                    "username": getattr(usuario, "username", str(usuario)),
                })
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass  # observabilidade nunca pode virar outra fonte de falha


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
    if exc is not None:
        _report_sentry(exc, pipeline=pipeline, evento=evento, level=level,
                       usuario=usuario, contexto=contexto)
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
