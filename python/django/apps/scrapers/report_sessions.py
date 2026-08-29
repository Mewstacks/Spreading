"""Sessões Playwright cifradas no banco, compartilhadas por web e workers.

O legado gravava arquivos no volume da máquina web. Desde que web e automação
foram separados em VMs, o worker nunca enxergava esses cookies. O banco é a fonte
autoritativa; um arquivo legado ainda é migrado no primeiro acesso feito pela web.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.crypto import decrypt
from apps.accounts.models import BrowserSession, organization_for_user


PROBE_FALHAS_PARA_DESCONECTAR = 3
VALID_PROVIDERS = frozenset(value for value, _ in BrowserSession.PROVIDERS)


def _provider(value: str) -> str:
    provider = str(value or "").strip().casefold()
    if provider not in VALID_PROVIDERS:
        raise ValueError("provedor de sessão de navegador inválido")
    return provider


def _directory(*, criar=False) -> Path:
    root = Path(getattr(settings, "ML_AUTH_DIR", "") or settings.BASE_DIR / "sessions")
    path = root / "report_sessions"
    if criar:
        path.mkdir(parents=True, exist_ok=True)
    return path


def encrypted_state_path(usuario, marketplace: str, *, criar=False) -> Path:
    """Caminho legado, mantido apenas para migração e remoção compatível."""
    return _directory(criar=criar) / f"{_provider(marketplace)}_{usuario.id}.state"


def _query(usuario, marketplace: str):
    organization = organization_for_user(usuario)
    if organization is None:
        return BrowserSession.objects.none()
    return BrowserSession.objects.filter(
        organization=organization, user=usuario, provider=_provider(marketplace),
    )


def _legacy_state(usuario, marketplace: str) -> dict | None:
    source = encrypted_state_path(usuario, marketplace)
    if not source.is_file():
        return None
    try:
        encoded = decrypt(source.read_text(encoding="utf-8"))
        raw = base64.b64decode(encoded.encode()).decode()
        state = json.loads(raw)
    except Exception as exc:
        raise ValueError("sessão de navegador legada ilegível; conecte novamente") from exc
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise ValueError("sessão de navegador legada inválida; conecte novamente")
    return state


def _migrate_legacy(usuario, marketplace: str) -> BrowserSession | None:
    state = _legacy_state(usuario, marketplace)
    if state is None:
        return None
    save_report_state(usuario, marketplace, state)
    try:
        encrypted_state_path(usuario, marketplace).unlink(missing_ok=True)
        encrypted_state_path(usuario, marketplace).with_suffix(".probe").unlink(
            missing_ok=True,
        )
    except OSError:
        pass
    return _query(usuario, marketplace).only(
        "status", "probe_failures",
    ).first()


def _record_or_migrate(usuario, marketplace: str, *, state=False):
    fields = (
        ("encrypted_state", "status", "probe_failures", "probe_result",
         "probe_reason", "last_probe_at")
        if state else
        ("status", "probe_failures", "probe_result", "probe_reason", "last_probe_at")
    )
    record = _query(usuario, marketplace).only(*fields).first()
    return record or _migrate_legacy(usuario, marketplace)


def has_report_session(usuario, marketplace: str) -> bool:
    try:
        record = _record_or_migrate(usuario, marketplace)
    except ValueError:
        return False
    return bool(
        record
        and record.status != "decrypt_error"
        and record.probe_failures < PROBE_FALHAS_PARA_DESCONECTAR
    )


def probe_snapshot(usuario, marketplace: str) -> dict:
    try:
        record = _record_or_migrate(usuario, marketplace)
    except ValueError:
        return {"falhas": PROBE_FALHAS_PARA_DESCONECTAR,
                "resultado": "suspeito", "motivo": "decrypt_error"}
    if record is None:
        return {"falhas": 0, "resultado": "", "motivo": ""}
    return {
        "falhas": int(record.probe_failures or 0),
        "resultado": str(record.probe_result or ""),
        "motivo": str(record.probe_reason or ""),
    }


def registrar_veredito(usuario, marketplace: str, resultado: str,
                        motivo: str = "") -> dict:
    from core.logging import redact_log_text

    provider = _provider(marketplace)
    organization = organization_for_user(usuario)
    if organization is None:
        return {"falhas": 0, "resultado": "", "motivo": ""}
    with transaction.atomic():
        record = BrowserSession.objects.select_for_update().filter(
            organization=organization, user=usuario, provider=provider,
        ).only("id", "probe_failures").first()
        if record is None:
            return {"falhas": 0, "resultado": "", "motivo": ""}
        failures = 0 if resultado == "conectado" else int(record.probe_failures) + 1
        reason = redact_log_text(motivo or "")[:200]
        record.probe_failures = failures
        record.probe_result = str(resultado or "")[:24]
        record.probe_reason = reason
        record.last_probe_at = timezone.now()
        record.status = "active" if resultado == "conectado" else "suspect"
        record.save(update_fields=(
            "probe_failures", "probe_result", "probe_reason", "last_probe_at",
            "status", "updated_at",
        ))
    return {"falhas": failures, "resultado": str(resultado or "")[:24],
            "motivo": reason}


def limpar_veredito(usuario, marketplace: str) -> None:
    _query(usuario, marketplace).update(
        status="active", probe_failures=0, probe_result="", probe_reason="",
        last_probe_at=None,
    )


def save_report_state(usuario, marketplace: str, state: dict) -> None:
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise ValueError("estado de sessão de navegador inválido")
    organization = organization_for_user(usuario)
    if organization is None:
        raise ValueError("usuário sem organização ativa")
    raw = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
    BrowserSession.objects.update_or_create(
        organization=organization, user=usuario, provider=_provider(marketplace),
        defaults={
            "encrypted_state": raw, "status": "active", "probe_failures": 0,
            "probe_result": "", "probe_reason": "", "last_probe_at": None,
            "last_used_at": timezone.now(),
        },
    )


def load_report_state(usuario, marketplace: str) -> dict | None:
    try:
        record = _record_or_migrate(usuario, marketplace, state=True)
        if record is None:
            return None
        state = json.loads(record.encrypted_state)
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
            raise ValueError("conteúdo inválido")
    except Exception as exc:
        try:
            _query(usuario, marketplace).update(status="decrypt_error")
        except Exception:
            pass
        raise ValueError("sessão de navegador ilegível; conecte novamente") from exc
    _query(usuario, marketplace).update(last_used_at=timezone.now())
    return state


def delete_report_state(usuario, marketplace: str) -> None:
    _query(usuario, marketplace).delete()
    for path in (
        encrypted_state_path(usuario, marketplace),
        encrypted_state_path(usuario, marketplace).with_suffix(".probe"),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
