"""Formatação de logs fail-safe: segredos nunca chegam ao console/Sentry breadcrumbs."""

from __future__ import annotations

import logging
import re


_URL_QUERY = re.compile(r"(https?://[^\s?'\"<>]+)\?[^\s'\"<>]+", re.I)
_SENSITIVE_FIELD = re.compile(
    r"(?i)((?:['\"])?(?:authorization|cookie|token|capability|password|"
    r"storage[_ -]?state)(?:['\"])?\s*[:=]\s*)"
    r"(?:['\"][^'\"]*['\"]|[^\s,;]+)"
)
_BEARER = re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I)
_PHONE = re.compile(
    r"(?<![\d-])(?:\+\d[\d\s().-]{8,}\d|"
    r"\(?\d{2}\)?\s*9?\d{4}[-\s]?\d{4}|\d{10,15})(?!\d)"
)
_LONG_BINARY = re.compile(r"\b[A-Za-z0-9+/]{256,}={0,2}\b")


def redact_log_text(value) -> str:
    text = str(value or "")
    text = _URL_QUERY.sub(r"\1?[redacted]", text)
    text = _BEARER.sub(r"\1[redacted]", text)
    text = _SENSITIVE_FIELD.sub(r"\1[redacted]", text)
    text = _PHONE.sub("[identificador mascarado]", text)
    return _LONG_BINARY.sub("[conteudo binario removido]", text)


class RedactingFormatter(logging.Formatter):
    """Redige a mensagem final, inclusive traceback e texto de exceção."""

    def format(self, record):
        return redact_log_text(super().format(record))
