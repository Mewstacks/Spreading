"""API oficial do ML (client_credentials, sem login). Nunca levanta; devolve vazio em falha."""
import logging
import threading
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)
BASE = "https://api.mercadolibre.com"
RENEW_MARGIN_S = 600
BACKOFF_S = 300

_LOCK = threading.Lock()
_TOKEN = {"value": "", "expires": 0.0, "silent_until": 0.0}


def configured() -> bool:
    return bool(settings.ML_API_CLIENT_ID and settings.ML_API_CLIENT_SECRET)


def reset_token_cache():
    with _LOCK:
        _TOKEN.update(value="", expires=0.0, silent_until=0.0)


def access_token(http=None) -> str:
    if not configured():
        return ""
    http = http or requests
    now = time.monotonic()
    with _LOCK:
        if _TOKEN["value"] and now < _TOKEN["expires"]:
            return _TOKEN["value"]
        if now < _TOKEN["silent_until"]:
            return ""
        try:
            response = http.post(
                f"{BASE}/oauth/token", timeout=settings.HTTP_TIMEOUT_SECONDS,
                headers={"accept": "application/json"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.ML_API_CLIENT_ID,
                    "client_secret": settings.ML_API_CLIENT_SECRET,
                },
            )
            body = response.json()
            if response.status_code != 200 or not body.get("access_token"):
                raise RuntimeError(f"token {response.status_code}")
        except Exception as exc:  # noqa: BLE001
            _TOKEN["silent_until"] = now + BACKOFF_S
            logger.warning("token da API do ML recusado: %s", exc)
            return ""
        _TOKEN["value"] = body["access_token"]
        _TOKEN["expires"] = now + max(60, int(body.get("expires_in") or 21600) - RENEW_MARGIN_S)
        return _TOKEN["value"]


def _get(path: str, params=None, http=None) -> dict:
    http = http or requests
    token = access_token(http)
    if not token:
        return {}
    try:
        response = http.get(
            f"{BASE}{path}", params=params, timeout=settings.HTTP_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {token}"},
        )
    except requests.exceptions.RequestException as exc:
        logger.info("API do ML não respondeu em %s: %s", path, str(exc)[:100])
        return {}
    if response.status_code == 401:
        reset_token_cache()
        return {}
    if response.status_code != 200:
        return {}
    try:
        return response.json() or {}
    except ValueError:
        return {}


def search_products(query: str, limit: int = 20, http=None) -> list[dict]:
    data = _get(
        "/products/search",
        {"status": "active", "site_id": "MLB", "q": query, "limit": limit},
        http,
    )
    return [r for r in (data.get("results") or []) if r.get("id")]


def product(product_id: str, http=None) -> dict:
    """Nome, primeira foto e estado do produto de catálogo; {} se a API não o devolve."""
    data = _get(f"/products/{product_id}", None, http)
    name = str(data.get("name") or "").strip()
    if not name:
        return {}
    pictures = data.get("pictures") or []
    first = pictures[0] if pictures and isinstance(pictures[0], dict) else {}
    return {"name": name, "image": str(first.get("url") or ""),
            "status": str(data.get("status") or "")}


def product_price(product_id: str, http=None) -> dict:
    """Preço do ganhador do buy box (`kvs_primary`); sem a marca, a primeira oferta."""
    data = _get(f"/products/{product_id}/items", None, http)
    offers = [o for o in (data.get("results") or []) if float(o.get("price") or 0) > 0]
    if not offers:
        return {}
    winner = next((o for o in offers if "kvs_primary" in (o.get("tags") or [])), offers[0])
    return {
        "price": float(winner["price"]),
        "price_from": float(winner.get("original_price") or 0),
        "item_id": winner.get("item_id") or "",
        "winner_tagged": "kvs_primary" in (winner.get("tags") or []),
    }
