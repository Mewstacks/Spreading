"""Cliente somente-leitura da Fly Machines API p/ o painel de infra do superadmin.

Infra é COMPARTILHADA (uma máquina por serviço, não por usuário) — este módulo só
expõe o estado global das máquinas Fly p/ o superadmin ver saúde/custo. Não provisiona
nada. Sem FLY_API_TOKEN (dev) devolve estado "indisponível" sem quebrar a página.
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_CACHE_KEY = "fly_infra_snapshot"
_CACHE_TTL = 60  # segundos — evita bater na API Fly a cada refresh do painel
_TIMEOUT = 6


def _listar_maquinas(app: str) -> list[dict]:
    url = f"{settings.FLY_API_HOST}/v1/apps/{app}/machines"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {settings.FLY_API_TOKEN}"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    dados = resp.json()
    maquinas = []
    for m in dados:
        guest = (m.get("config") or {}).get("guest") or {}
        maquinas.append({
            "id": m.get("id", ""),
            "nome": m.get("name", ""),
            "estado": m.get("state", "?"),
            "regiao": m.get("region", "?"),
            "cpus": guest.get("cpus"),
            "cpu_kind": guest.get("cpu_kind", ""),
            "memoria_mb": guest.get("memory_mb"),
        })
    return maquinas


def snapshot(force: bool = False) -> dict:
    """Estado das máquinas Fly p/ os apps configurados. Cacheado 60s.

    Retorno: {"disponivel": bool, "motivo": str, "custo_mensal": str, "apps": [...]}
    Cada app: {"app": nome, "ok": bool, "erro": str, "maquinas": [...]}.
    """
    if not settings.FLY_API_TOKEN:
        return {
            "disponivel": False,
            "motivo": "FLY_API_TOKEN não configurado (esperado em dev).",
            "custo_mensal": settings.FLY_CUSTO_MENSAL_USD,
            "apps": [],
        }

    if not force:
        cached = cache.get(_CACHE_KEY)
        if cached is not None:
            return cached

    apps = []
    for app in settings.FLY_APPS:
        try:
            apps.append({"app": app, "ok": True, "erro": "",
                         "maquinas": _listar_maquinas(app)})
        except Exception as e:  # rede/token/app inexistente — degrada sem quebrar
            logger.warning("Fly infra: falha ao listar %s: %s", app, e)
            apps.append({"app": app, "ok": False, "erro": str(e), "maquinas": []})

    snap = {
        "disponivel": True,
        "motivo": "",
        "custo_mensal": settings.FLY_CUSTO_MENSAL_USD,
        "apps": apps,
    }
    cache.set(_CACHE_KEY, snap, _CACHE_TTL)
    return snap
