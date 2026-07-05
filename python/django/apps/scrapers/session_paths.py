"""Caminho único das sessões do Mercado Livre (auth.json / auth_{id}.json).

Centraliza a resolução do diretório p/ que produção (Fly.io) grave num VOLUME
persistente via settings.ML_SESSION_DIR, sem espalhar `os.path.join(caminho_atual,
"auth.json")` por vários módulos. Dev cai na pasta do scraper_mercadolivre.
"""
import os

from django.conf import settings

# Fallback dev: mesma pasta onde os arquivos historicamente ficavam.
_DEFAULT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "scraper_mercadolivre"
)


def ml_session_dir() -> str:
    """Diretório das sessões do ML. Cria se não existir (volume novo no Fly)."""
    d = getattr(settings, "ML_SESSION_DIR", "") or _DEFAULT_DIR
    os.makedirs(d, exist_ok=True)
    return d


def ml_auth_path(user=None) -> str:
    """auth_{id}.json do usuário se existir; senão o auth.json global."""
    d = ml_session_dir()
    if user is not None and getattr(user, "id", None):
        p = os.path.join(d, f"auth_{user.id}.json")
        if os.path.exists(p):
            return p
    return os.path.join(d, "auth.json")
