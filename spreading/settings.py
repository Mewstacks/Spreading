"""Settings de produção/dev: falha fechada se a chave secreta for fraca (fora do DEBUG)."""
from .base import *  # noqa: F403
from .base import DEBUG, env

SECRET_KEY = env("DJANGO_SECRET_KEY", "")
_KNOWN_PLACEHOLDERS = {"dev-only-insecure-key-change-me", "troque-por-uma-chave-longa", "build"}

if DEBUG:
    SECRET_KEY = SECRET_KEY or "debug-only-insecure-key"
elif len(SECRET_KEY) < 50 or SECRET_KEY in _KNOWN_PLACEHOLDERS or "troque" in SECRET_KEY.lower():
    raise RuntimeError("DJANGO_SECRET_KEY ausente, curta (<50) ou placeholder: defina uma real")
