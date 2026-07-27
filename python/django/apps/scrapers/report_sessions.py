"""Sessões cifradas dos portais de relatório por usuário e marketplace.

O storage state do Playwright contém cookies de autenticação. Para relatórios ele
nunca fica em JSON legível no volume: o arquivo persistido usa Fernet e o
plaintext é entregue ao Playwright somente como objeto em memória.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from django.conf import settings

from apps.accounts.crypto import decrypt, encrypt


def _directory() -> Path:
    root = Path(getattr(settings, "ML_AUTH_DIR", "") or settings.BASE_DIR / "sessions")
    path = root / "report_sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def encrypted_state_path(usuario, marketplace: str) -> Path:
    return _directory() / f"{marketplace}_{usuario.id}.state"


def has_report_session(usuario, marketplace: str) -> bool:
    return encrypted_state_path(usuario, marketplace).is_file()


def save_report_state(usuario, marketplace: str, state: dict) -> None:
    raw = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
    cipher = encrypt(base64.b64encode(raw.encode()).decode())
    target = encrypted_state_path(usuario, marketplace)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(cipher, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)


def load_report_state(usuario, marketplace: str) -> dict | None:
    """Descriptografa em memória; nunca materializa cookies em arquivo temporário."""
    source = encrypted_state_path(usuario, marketplace)
    if not source.is_file():
        return None
    try:
        encoded = decrypt(source.read_text(encoding="utf-8"))
        raw = base64.b64decode(encoded.encode()).decode()
        state = json.loads(raw)
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
            raise ValueError("estado de sessão inválido")
    except Exception as exc:
        raise ValueError("sessão de relatórios ilegível; conecte novamente") from exc

    return state
