"""Caminho único das sessões do Mercado Livre (auth.json / auth_{id}.json).

Centraliza a resolução do diretório p/ que produção (Fly.io) grave num VOLUME
persistente via settings.ML_AUTH_DIR, sem espalhar `os.path.join(caminho_atual,
"auth.json")` por vários módulos. É o MESMO diretório onde a conexão web
(ml_conexao.py — Chromium local + live view) grava o storage_state — tudo lê do mesmo lugar.
"""
import os

from django.conf import settings

# Fallback dev: mesma pasta onde os arquivos historicamente ficavam.
_DEFAULT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "scraper_mercadolivre"
)


def ml_session_dir() -> str:
    """Diretório das sessões do ML. Cria se não existir (volume novo no Fly)."""
    d = getattr(settings, "ML_AUTH_DIR", "") or _DEFAULT_DIR
    os.makedirs(d, exist_ok=True)
    return d


def ml_auth_path(user=None) -> str:
    """Caminho da sessão do ML a usar.

    Com usuário: o auth_{id}.json dele — é o que a tela de conexão grava
    (ml_conexao.py). Sem usuário (jobs de cron: mapear_cupons, prefetch), cai na
    ordem: auth.json legado, senão o auth_{id}.json mais recente.

    O fallback existe porque a conexão é POR USUÁRIO mas o pool de produtos do ML
    é COMPARTILHADO (owner=None): um job sem request ainda precisa de uma sessão
    viva. Consequência: o link sai com a tag de afiliado do dono da sessão
    escolhida. Com um só usuário conectado é o comportamento óbvio; com vários,
    é uma decisão de produto a rever.
    """
    d = ml_session_dir()
    if user is not None and getattr(user, "id", None):
        return os.path.join(d, f"auth_{user.id}.json")

    legado = os.path.join(d, "auth.json")
    if os.path.exists(legado):
        return legado

    try:
        candidatos = [
            os.path.join(d, n) for n in os.listdir(d)
            if n.startswith("auth_") and n.endswith(".json")
        ]
    except OSError:
        candidatos = []
    if candidatos:
        return max(candidatos, key=os.path.getmtime)

    # Nenhuma sessão: devolve o caminho legado p/ o chamador reportar
    # "desconecte/reconecte" em vez de estourar aqui.
    return legado
