"""API oficial do Mercado Livre — preço sem sessão de navegador.

Por que existe
-------------
A medição de preço no envio dependia da página web: GET com os cookies do
`storage_state`. Desde 08/2026 o ML responde challenge ao IP de datacenter da
Fly na PDP, então `preco_ao_vivo` devolvia `fonte=inconclusivo` e o envio de
deal parava — "preço não confirmado no envio: sem dado ao vivo". A vitrine
`/ofertas` ainda responde, mas o card não confirma vendedor, variação nem
cupom: foi o card que anunciou R$ 93,60 num item que o checkout cobrava R$ 117.

`api.mercadolibre.com` é outra porta: não passa pelo anti-bot do site, devolve
o preço estruturado e não depende de nenhum Chromium.

Autenticação
------------
`client_credentials`, medido contra o app real em 22/09/2026: devolve token de
6h com `scope: read`, sem nenhum login. A doc pública de Autenticação
(atualizada em 29/12/2025) diz que só `authorization_code` e `refresh_token`
são aceitos — ela está defasada em relação ao painel de aplicações, que oferece
o fluxo e o honra. Como é a doc que pode voltar a valer, um 400
`unsupported_grant_type` aqui não é bug deste módulo: é a porta fechando, e a
fonte simplesmente deixa de responder (o caminho antigo continua atrás dela).

O token vive em memória do processo. Não vai para o banco de propósito: sem
refresh token não há nada de uso único para coordenar entre as VMs, e cada
worker pega o seu em uma chamada.

O que responde e o que não responde (medido em produção, 22/09/2026)
--------------------------------------------------------------------
    GET /products/{id}/items   200  — ofertas do catálogo, com preço por vendedor
    GET /items/{id}            403  access_denied em anúncio de terceiro
    GET /sites/MLB/search      403  forbidden

Ou seja: link de catálogo (`/p/MLB…`) é medível; anúncio solto (`/MLB-…`) não é,
e para ele o caminho antigo (GET com cookies) segue sendo a única porta.
"""
import logging
import threading
import time

from django.conf import settings

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
ITEM_URL = "https://api.mercadolibre.com/items/{item_id}"
OFERTAS_DO_PRODUTO_URL = "https://api.mercadolibre.com/products/{product_id}/items"

TIMEOUT_S = 10
# Renova antes de vencer: token que expira no meio da chamada vira 401 e gasta
# um candidato do tique de envio.
MARGEM_RENOVACAO_S = 600
# Silêncio depois de falha de credencial. Sem isto cada candidato do tique
# repete a mesma troca de token contra a mesma recusa.
RECUO_FALHA_S = 300

# `silencio_ate` guarda o INSTANTE em que o recuo termina, não o instante da
# falha. No Linux `time.monotonic()` é o uptime da máquina, então guardar a
# falha e comparar `agora - falhou_em < RECUO` tratava o zero inicial como
# "falhou agora" e calava a fonte nos primeiros 5 minutos de vida de cada VM —
# exatamente a janela depois de todo deploy.
_LOCK = threading.Lock()
_TOKEN = {"valor": "", "expira_em": 0.0, "silencio_ate": 0.0}


def configurado() -> bool:
    return bool(getattr(settings, "ML_API_CLIENT_ID", "")
                and getattr(settings, "ML_API_CLIENT_SECRET", ""))


def _pedir_token() -> dict:
    import requests

    resposta = requests.post(
        TOKEN_URL, timeout=TIMEOUT_S,
        headers={"accept": "application/json",
                 "content-type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": getattr(settings, "ML_API_CLIENT_ID", ""),
            "client_secret": getattr(settings, "ML_API_CLIENT_SECRET", ""),
        },
    )
    corpo = {}
    try:
        corpo = resposta.json()
    except ValueError:
        pass
    if resposta.status_code != 200:
        # `unsupported_grant_type` aqui significa que o ML fechou o fluxo sem
        # usuário; `invalid_client`, que o segredo mudou. São diagnósticos
        # opostos e vale distinguir no log.
        raise RuntimeError(
            f"{resposta.status_code} {corpo.get('error') or ''} "
            f"{corpo.get('error_description') or ''}".strip()[:180]
        )
    return corpo


def access_token() -> str:
    """Token válido em cache de processo. '' quando a fonte não está disponível.

    Nunca levanta: isto roda dentro do tique de envio, e credencial ausente é
    "esta fonte não respondeu", não erro de envio.
    """
    if not configurado():
        return ""
    agora = time.monotonic()
    with _LOCK:
        if _TOKEN["valor"] and agora < _TOKEN["expira_em"]:
            return _TOKEN["valor"]
        if agora < _TOKEN["silencio_ate"]:
            return ""
        try:
            corpo = _pedir_token()
        except Exception as exc:
            _TOKEN["silencio_ate"] = agora + RECUO_FALHA_S
            logger.warning("Token da API do ML recusado: %s", exc)
            return ""
        _TOKEN["valor"] = corpo.get("access_token") or ""
        _TOKEN["expira_em"] = agora + max(
            60, int(corpo.get("expires_in") or 21600) - MARGEM_RENOVACAO_S)
        _TOKEN["silencio_ate"] = 0.0
        return _TOKEN["valor"]


def _get(url: str) -> dict:
    import requests

    token = access_token()
    if not token:
        return {}
    try:
        resposta = requests.get(
            url, timeout=TIMEOUT_S, headers={"Authorization": f"Bearer {token}"},
        )
    except Exception as exc:
        logger.info("API do ML não respondeu (%s): %s", url[-40:], str(exc)[:120])
        return {}
    if resposta.status_code == 401:
        # Token revogado no meio do caminho: descarta e deixa a próxima chamada
        # pegar outro, em vez de repetir 401 por candidato.
        with _LOCK:
            _TOKEN["valor"] = ""
            _TOKEN["expira_em"] = 0.0
        return {}
    if resposta.status_code != 200:
        logger.info("API do ML devolveu %s em %s", resposta.status_code, url[-40:])
        return {}
    try:
        return resposta.json() or {}
    except ValueError:
        return {}


def preco_do_item(item_id: str) -> dict:
    """Preço de um anúncio solto. Hoje 403 para vendedor terceiro — ver o topo."""
    if not item_id:
        return {}
    dados = _get(ITEM_URL.format(item_id=item_id))
    if not dados or dados.get("status") != "active":
        return {}
    preco = float(dados.get("price") or 0)
    if preco <= 0:
        return {}
    return {"preco": preco, "preco_de": float(dados.get("original_price") or 0),
            "item_id": dados.get("id") or item_id}


def preco_do_produto(product_id: str) -> dict:
    """Preço do anúncio ganhador do buy box de um produto de catálogo.

    O comprador que abre `/p/MLB…` vê UMA oferta — a que ganhou o buy box —, não
    a mais barata da lista. `kvs_primary` é a marca dessa oferta; sem ela, a
    ordem devolvida pela API é a mesma da página. Pegar a menor seria anunciar
    um preço que o link publicado não abre.
    """
    if not product_id:
        return {}
    dados = _get(OFERTAS_DO_PRODUTO_URL.format(product_id=product_id))
    ofertas = [o for o in (dados.get("results") or [])
               if float(o.get("price") or 0) > 0]
    if not ofertas:
        return {}
    ganhador = next(
        (o for o in ofertas if "kvs_primary" in (o.get("tags") or [])), ofertas[0])
    return {
        "preco": float(ganhador["price"]),
        "preco_de": float(ganhador.get("original_price") or 0),
        "item_id": ganhador.get("item_id") or "",
    }
