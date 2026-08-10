"""Diagnósticos saneados apenas de fontes públicas, com retenção curta."""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from django.conf import settings
from django.utils import timezone


_SENSITIVE = re.compile(
    r"(?i)(authorization|cookie|token|session|storage[_ -]?state|capability|password)\s*[:=]\s*[^\s<]+"
)
_QUERY = re.compile(r"(https?://[^\s<>'\"]+)\?[^\s<>'\"]+")


def _safe_text(value):
    text = _QUERY.sub(r"\1?[redacted]", str(value or ""))
    return _SENSITIVE.sub(r"\1=[redacted]", text)[:100_000]


def _directory():
    path = Path(tempfile.gettempdir()) / "spreading-public-source-diagnostics"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def prune_public_diagnostics():
    cutoff = timezone.now().timestamp() - max(
        1, int(settings.SCRAPER_DIAGNOSTIC_RETENTION_DAYS)
    ) * 86400
    removed = 0
    for entry in _directory().iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def capture_public_diagnostic(page, slug, reason):
    """Guarda texto e screenshot públicos; nunca deve receber sessão autenticada."""
    prune_public_diagnostics()
    stamp = timezone.now().strftime("%Y%m%dT%H%M%S%fZ")
    base = _directory() / f"{re.sub(r'[^a-z0-9_-]', '-', slug.lower())}-{stamp}"
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        text = ""
    payload = _safe_text(f"reason={reason}\n{text}")
    tmp = base.with_suffix(".txt.tmp")
    final = base.with_suffix(".txt")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        data = payload.encode("utf-8", errors="replace")
        written = 0
        while written < len(data):
            written += os.write(descriptor, data[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(tmp, final)
    try:
        page.screenshot(path=str(base.with_suffix(".png")), full_page=False)
        base.with_suffix(".png").chmod(0o600)
    except Exception:
        pass
    return str(final)


def capture_public_text_diagnostic(text, slug, reason):
    """Versão para fontes HTTP públicas sem navegador."""
    prune_public_diagnostics()
    stamp = timezone.now().strftime("%Y%m%dT%H%M%S%fZ")
    base = _directory() / f"{re.sub(r'[^a-z0-9_-]', '-', slug.lower())}-{stamp}.txt"
    tmp = base.with_suffix(".txt.tmp")
    payload = _safe_text(f"reason={reason}\n{text}").encode("utf-8", errors="replace")
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(tmp, base)
    return str(base)
