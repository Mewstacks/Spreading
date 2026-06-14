"""
Cliente fino da Amazon Creators API (sucessor da PA-API 5.0, desligada 2026-05-15).

Multi-tenant: CADA usuário conecta a PRÓPRIA conta Amazon (credential id/secret/tag).
As funções recebem `creds` (Credenciais); sem isso caem nas credenciais globais de
settings (dev / conta do app). O token OAuth2 é cacheado POR credencial.

Diferenças vs PA-API 5.0:
  - Auth OAuth2 (Credential ID + Secret -> bearer token ~1h) em vez de AWS SigV4.
  - Host de endpoint próprio; payloads em lowerCamelCase (itemIds, searchIndex, ...).

Throttle: ≤1 TPS por processo. Em 403 AssociateNotEligible levanta AmazonNotEligible
para o chamador pular a Amazon daquele usuário sem derrubar o resto.

OBS: o schema exato (endpoints/campos) está atrás da doc autenticada da Amazon. O
parsing de resposta fica isolado em ofertas_scraper._mapear_item; aqui só transporte.
"""
import threading
import time
from dataclasses import dataclass

import requests
from django.conf import settings


class AmazonConfigError(Exception):
    """Credenciais/host da Creators API não configurados (usuário ou settings)."""


class AmazonNotEligible(Exception):
    """403 AssociateNotEligible — conta sem 10 vendas qualificadas/30 dias."""


class AmazonAPIError(Exception):
    """Erro genérico de chamada à Creators API."""


@dataclass(frozen=True)
class Credenciais:
    credential_id: str
    credential_secret: str
    host: str
    partner_tag: str
    marketplace: str = "www.amazon.com.br"

    def chave(self) -> str:
        """Chave de cache do token (credencial identifica a conta)."""
        return self.credential_id or "global"

    def completo(self) -> bool:
        return all((self.credential_id, self.credential_secret, self.host, self.partner_tag))


# Token OAuth2 cacheado POR credencial: {chave: {"token","exp"}}. Lock p/ corridas.
_token_cache: dict = {}
_token_lock = threading.Lock()
_ultima_chamada = {"t": 0.0}  # throttle simples ≤1 TPS (por processo)


def _cfg(nome):
    return (getattr(settings, nome, "") or "")


def creds_globais() -> Credenciais:
    """Credenciais do app (settings) — fallback/dev e fonte única quando sem usuário."""
    return Credenciais(
        credential_id=_cfg("AMAZON_CREATOR_CREDENTIAL_ID"),
        credential_secret=_cfg("AMAZON_CREATOR_CREDENTIAL_SECRET"),
        host=_cfg("AMAZON_CREATORS_HOST"),
        partner_tag=_cfg("AMAZON_PARTNER_TAG"),
        marketplace=_cfg("AMAZON_MARKETPLACE") or "www.amazon.com.br",
    )


def creds_de_usuario(usuario) -> Credenciais:
    """Credenciais Amazon do usuário (Perfil); cai no global quando vazias."""
    if usuario is None:
        return creds_globais()
    perfil = getattr(usuario, "perfil", None)
    g = creds_globais()
    if not perfil:
        return g
    return Credenciais(
        credential_id=(perfil.amazon_credential_id or g.credential_id),
        credential_secret=(perfil.amazon_credential_secret or g.credential_secret),
        host=(perfil.amazon_creators_host or g.host),
        partner_tag=(perfil.afiliado_tag_amazon or g.partner_tag),
        marketplace=g.marketplace,
    )


def _resolver(creds) -> Credenciais:
    return creds if isinstance(creds, Credenciais) else creds_globais()


def _exigir(creds: Credenciais):
    if not creds.completo():
        raise AmazonConfigError(
            "Credenciais Amazon incompletas (credential id/secret/host/tag)."
        )


def _throttle():
    delta = time.time() - _ultima_chamada["t"]
    if delta < 1.05:
        time.sleep(1.05 - delta)
    _ultima_chamada["t"] = time.time()


def _obter_token(creds: Credenciais) -> str:
    """Bearer token válido p/ ESTA credencial, renovando ~60s antes de expirar."""
    chave = creds.chave()
    agora = time.time()
    cache = _token_cache.get(chave)
    if cache and agora < cache["exp"] - 60:
        return cache["token"]

    with _token_lock:
        agora = time.time()
        cache = _token_cache.get(chave)
        if cache and agora < cache["exp"] - 60:
            return cache["token"]

        _exigir(creds)
        try:
            r = requests.post(
                f"https://{creds.host}/auth/token",
                json={
                    "grantType": "client_credentials",
                    "clientId": creds.credential_id,
                    "clientSecret": creds.credential_secret,
                },
                timeout=15,
            )
        except Exception as e:
            raise AmazonAPIError(f"Falha ao obter token: {e}")

        if r.status_code in (401, 403):
            raise AmazonNotEligible(f"Auth recusada ({r.status_code}): {r.text[:200]}")
        if r.status_code >= 400:
            raise AmazonAPIError(f"Token HTTP {r.status_code}: {r.text[:200]}")

        data = r.json()
        token = data.get("accessToken") or data.get("access_token") or ""
        if not token:
            raise AmazonAPIError(f"Resposta de token sem accessToken: {data}")
        expires = data.get("expiresIn") or data.get("expires_in") or 3600
        _token_cache[chave] = {"token": token, "exp": time.time() + float(expires)}
        return token


def _post(operacao: str, payload: dict, creds: Credenciais) -> dict:
    """POST genérico autenticado para uma operação da Creators API (com as creds dadas)."""
    _exigir(creds)
    body = dict(payload)
    body.setdefault("partnerTag", creds.partner_tag)
    body.setdefault("partnerType", "Associates")
    body.setdefault("marketplace", creds.marketplace)

    _throttle()
    try:
        r = requests.post(
            f"https://{creds.host}/paapi5/{operacao}",
            json=body,
            headers={
                "Authorization": f"Bearer {_obter_token(creds)}",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
    except Exception as e:
        raise AmazonAPIError(f"{operacao} falhou: {e}")

    if r.status_code == 403 and "eligib" in r.text.lower():
        raise AmazonNotEligible(r.text[:200])
    if r.status_code == 429:
        time.sleep(2)
        return _post(operacao, payload, creds)
    if r.status_code >= 400:
        raise AmazonAPIError(f"{operacao} HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


# Resources pedidos em cada chamada (preço/savings/imagem/título/Prime/promoções).
_RESOURCES = [
    "itemInfo.title",
    "images.primary.large",
    "offersV2.listings.price",
    "offersV2.listings.savingBasis",
    "offersV2.listings.deliveryInfo.isPrimeEligible",
    "offersV2.listings.promotions",
    "browseNodeInfo.browseNodes",
]


def search_items(keywords: str, min_savings_percent=None, item_count: int = 10,
                 page: int = 1, creds=None) -> list:
    """Busca por palavra-chave com filtro opcional de desconto mínimo (%)."""
    creds = _resolver(creds)
    payload = {
        "keywords": keywords,
        "itemCount": max(1, min(int(item_count), 10)),  # Creators API: máx 10/página
        "itemPage": int(page),
        "resources": _RESOURCES,
    }
    if min_savings_percent:
        payload["minSavingPercent"] = int(min_savings_percent)
    data = _post("searchItems", payload, creds)
    return (data.get("searchResult", {}) or {}).get("items", []) or []


def get_items(asins, creds=None) -> list:
    """Lookup por ASIN (refresh de preço/disponibilidade / liveness)."""
    creds = _resolver(creds)
    ids = [a for a in (asins if isinstance(asins, (list, tuple)) else [asins]) if a]
    if not ids:
        return []
    data = _post("getItems", {"itemIds": ids[:10], "resources": _RESOURCES}, creds)
    return (data.get("itemsResult", {}) or {}).get("items", []) or []
