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
`client_credentials` NÃO é suportado pelo Mercado Livre: a doc de Autenticação
e Autorização (developers.mercadolivre.com.br, atualizada em 29/12/2025) lista
`authorization_code` e `refresh_token` como os únicos grant_type aceitos —
qualquer outro devolve `unsupported_grant_type`. Logo o dono autoriza o app
UMA vez pelo navegador dele, e a partir daí o servidor só renova:

    access_token  vale 6 h
    refresh_token vale 6 meses, é de USO ÚNICO e volta renovado a cada troca

Uso único é o detalhe perigoso: duas renovações concorrentes (web + worker)
queimam a credencial e derrubam a integração. Por isso a renovação acontece
dentro de `select_for_update()`, e quem perder a corrida relê a linha já
renovada pelo outro.
"""
import logging
import time
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
ITEM_URL = "https://api.mercadolibre.com/items/{item_id}"
AUTORIZACAO_URL = "https://auth.mercadolivre.com.br/authorization"

# Renova antes de vencer: um token que expira no meio da requisição vira 401 e
# gasta um candidato do tique de envio.
MARGEM_RENOVACAO_S = 600
TIMEOUT_S = 10
# Silêncio depois de falha de credencial. Sem isto, cada candidato do tique
# repete a mesma troca de token contra um refresh já queimado.
RECUO_FALHA_S = 300

_ULTIMA_FALHA = {"quando": 0.0}


def configurado() -> bool:
    return bool(getattr(settings, "ML_API_CLIENT_ID", "")
                and getattr(settings, "ML_API_CLIENT_SECRET", ""))


def _registro(*, para_escrita=False):
    from apps.scrapers.models import MLApiToken

    qs = MLApiToken.objects.all()
    if para_escrita:
        qs = qs.select_for_update()
    return qs.order_by("pk").first()


def autorizacao_url(redirect_uri: str, state: str = "") -> str:
    """URL que o dono abre UMA vez para autorizar o app na conta dele."""
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": getattr(settings, "ML_API_CLIENT_ID", ""),
        "redirect_uri": redirect_uri,
    }
    if state:
        params["state"] = state
    return f"{AUTORIZACAO_URL}?{urlencode(params)}"


def _post_token(dados: dict) -> dict:
    import requests

    resposta = requests.post(
        TOKEN_URL, data=dados, timeout=TIMEOUT_S,
        headers={"accept": "application/json",
                 "content-type": "application/x-www-form-urlencoded"},
    )
    corpo = {}
    try:
        corpo = resposta.json()
    except ValueError:
        pass
    if resposta.status_code != 200:
        # A mensagem do ML já é pública e sem segredo (`invalid_grant`,
        # `unsupported_grant_type`); guardá-la é o que permite distinguir
        # "app bloqueado" de "refresh queimado" sem abrir a conta.
        raise RuntimeError(
            f"{resposta.status_code} {corpo.get('error') or ''} "
            f"{corpo.get('error_description') or ''}".strip()[:180]
        )
    return corpo


def _gravar(corpo: dict):
    from apps.scrapers.models import MLApiToken

    expira = timezone.now() + timedelta(
        seconds=int(corpo.get("expires_in") or 21600))
    registro = _registro(para_escrita=True)
    campos = {
        "access_token": corpo.get("access_token") or "",
        "refresh_token": corpo.get("refresh_token") or "",
        "expira_em": expira,
        "conta_id": str(corpo.get("user_id") or ""),
        "ultimo_erro": "",
    }
    if registro is None:
        return MLApiToken.objects.create(**campos)
    for campo, valor in campos.items():
        # Um refresh vazio na resposta não pode apagar o que ainda funciona.
        if campo == "refresh_token" and not valor:
            continue
        setattr(registro, campo, valor)
    registro.save()
    return registro


def trocar_code(code: str, redirect_uri: str):
    """Primeira e única troca manual: authorization_code -> tokens."""
    with transaction.atomic():
        corpo = _post_token({
            "grant_type": "authorization_code",
            "client_id": getattr(settings, "ML_API_CLIENT_ID", ""),
            "client_secret": getattr(settings, "ML_API_CLIENT_SECRET", ""),
            "code": code,
            "redirect_uri": redirect_uri,
        })
        registro = _gravar(corpo)
    _ULTIMA_FALHA["quando"] = 0.0
    logger.info("API do ML autorizada para a conta %s", registro.conta_id)
    return registro


def access_token() -> str:
    """Token válido, renovando quando faltarem menos de 10 min. '' se não dá.

    Nunca levanta: isto roda dentro do tique de envio, e credencial ausente é
    "esta fonte não respondeu", não erro de envio.
    """
    if not configurado():
        return ""
    if time.monotonic() - _ULTIMA_FALHA["quando"] < RECUO_FALHA_S:
        return ""
    try:
        with transaction.atomic():
            registro = _registro(para_escrita=True)
            if registro is None or not registro.refresh_token:
                return ""
            vivo = (registro.access_token and registro.expira_em
                    and registro.expira_em - timezone.now()
                    > timedelta(seconds=MARGEM_RENOVACAO_S))
            if vivo:
                return registro.access_token
            corpo = _post_token({
                "grant_type": "refresh_token",
                "client_id": getattr(settings, "ML_API_CLIENT_ID", ""),
                "client_secret": getattr(settings, "ML_API_CLIENT_SECRET", ""),
                "refresh_token": registro.refresh_token,
            })
            registro = _gravar(corpo)
            return registro.access_token
    except Exception as exc:
        _ULTIMA_FALHA["quando"] = time.monotonic()
        logger.warning("Renovação do token da API do ML falhou: %s", exc)
        try:
            with transaction.atomic():
                registro = _registro(para_escrita=True)
                if registro is not None:
                    registro.ultimo_erro = str(exc)[:200]
                    registro.save(update_fields=["ultimo_erro", "atualizado_em"])
        except Exception:
            pass
        return ""


def item(item_id: str) -> dict:
    """GET /items/{id}. {} quando a fonte não respondeu ou o item sumiu."""
    import requests

    token = access_token()
    if not token or not item_id:
        return {}
    try:
        resposta = requests.get(
            ITEM_URL.format(item_id=item_id), timeout=TIMEOUT_S,
            headers={"Authorization": f"Bearer {token}"},
            params={"attributes": "id,price,original_price,status,"
                                  "available_quantity,permalink,seller_id"},
        )
    except Exception as exc:
        logger.info("API do ML não respondeu para %s: %s", item_id, str(exc)[:120])
        return {}
    if resposta.status_code == 401:
        # Token revogado no meio do caminho: recua e deixa a próxima janela
        # renovar, em vez de repetir 401 por candidato.
        _ULTIMA_FALHA["quando"] = time.monotonic()
        return {}
    if resposta.status_code != 200:
        logger.info("API do ML devolveu %s para %s", resposta.status_code, item_id)
        return {}
    try:
        return resposta.json() or {}
    except ValueError:
        return {}


def preco_do_item(item_id: str) -> dict:
    """{'preco', 'preco_de'} do anúncio ativo, ou {} quando não dá para afirmar.

    Item pausado/encerrado devolve {} de propósito: a lane de envio trata isso
    como "não mede agora", e quem decide se o anúncio morreu é a verificação de
    liveness, não esta função.
    """
    dados = item(item_id)
    if not dados or dados.get("status") != "active":
        return {}
    preco = float(dados.get("price") or 0)
    if preco <= 0:
        return {}
    return {"preco": preco, "preco_de": float(dados.get("original_price") or 0)}
