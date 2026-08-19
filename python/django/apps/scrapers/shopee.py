"""Cliente da Shopee Affiliate Open API (GraphQL assinado).

Por que esta loja importa mais do que "mais uma loja": ela é a primeira do
Spreading com API oficial para TODAS as etapas. No Mercado Livre não existe API de
afiliados — cada link sai do Link Builder dentro de um Chromium, o que faz o custo
crescer com ``cupons × usuários`` e transformou o navegador no gargalo de produção.
Aqui, ``generateShortLink`` devolve o link comissionado numa chamada HTTP. Nenhum
slot de navegador é consumido, então a Shopee escala com o número de clientes sem
disputar o recurso mais escasso da máquina.

Autenticação (documentação oficial do programa de afiliados):

    Authorization: SHA256 Credential={AppId}, Timestamp={ts}, Signature={sig}
    sig = sha256(AppId + Timestamp + Payload + Secret)

O ``Payload`` é o corpo JSON EXATAMENTE como vai no fio — por isso o corpo é
serializado uma única vez e a mesma string é usada para assinar e para enviar.
Reserializar (por exemplo, deixando o ``requests`` fazer isso com ``json=``) muda
espaçamento e ordem e invalida a assinatura silenciosamente, com um 401 que parece
credencial errada.

Multi-tenant: cada organização conecta a própria conta. As credenciais vivem em
``IntegracaoAfiliado`` (provedor ``shopee``), com o Secret no campo cifrado
``token`` — mesmo contrato já usado pela Awin.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

API_URL = "https://open-api.affiliate.shopee.com.br/graphql"

# (connect, read). A API responde rápido; o timeout curto evita que uma indisponi-
# bilidade da Shopee segure um worker que também atende outras lojas.
_TIMEOUT = (5, 20)

# Código de limite de taxa documentado. Não é erro de credencial nem de query: é
# "diminua o ritmo", e tratá-lo como falha permanente derrubaria a fonte no circuit
# breaker por meia hora sem necessidade.
RATE_LIMITED = 10030

PAGE_SIZE = 50
MAX_PAGES = 20


class ShopeeError(Exception):
    """Falha explicável ao usuário. Nunca carrega token, assinatura nem corpo cru."""

    def __init__(self, public_message, *, retryable=False, code=0):
        super().__init__(public_message)
        self.public_message = public_message
        self.retryable = retryable
        self.code = code


class ShopeeConfigError(ShopeeError):
    """Credenciais ausentes ou incompletas para esta organização."""


def _assinar(app_id: str, secret: str, payload: str, timestamp: int) -> str:
    base = f"{app_id}{timestamp}{payload}{secret}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _mensagem_publica(code: int, texto: str) -> str:
    if code == RATE_LIMITED:
        return "A Shopee pediu para reduzir o ritmo; a coleta continua no próximo ciclo."
    if code in (10020, 10021, 10022):
        return "Credenciais da Shopee recusadas. Reconecte a conta de afiliado."
    if "signature" in texto.lower():
        return "Assinatura recusada pela Shopee. Reconecte a conta de afiliado."
    return "A Shopee não respondeu a esta consulta."


def executar(query: str, variables=None, *, app_id: str, secret: str):
    """Executa uma query GraphQL assinada e devolve o bloco ``data``.

    Erros do GraphQL chegam com HTTP 200 e um array ``errors`` — checar só o status
    deixaria passar resposta vazia como se fosse sucesso, que é como uma fonte
    quebrada vira "coleta vazia" e apaga catálogo. Aqui qualquer ``errors`` vira
    exceção, e o chamador decide se preserva o que já tinha.
    """
    if not app_id or not secret:
        raise ShopeeConfigError("Conecte a conta de afiliado da Shopee.")

    corpo = {"query": query, "variables": variables or {}}
    # Serializa UMA vez: a mesma string assina e viaja (ver docstring do módulo).
    payload = json.dumps(corpo, separators=(",", ":"), ensure_ascii=False)
    timestamp = int(time.time())
    cabecalhos = {
        "Content-Type": "application/json",
        "Authorization": (
            f"SHA256 Credential={app_id}, Timestamp={timestamp}, "
            f"Signature={_assinar(app_id, secret, payload, timestamp)}"
        ),
    }
    try:
        resposta = requests.post(
            API_URL, data=payload.encode("utf-8"), headers=cabecalhos, timeout=_TIMEOUT,
        )
    except requests.Timeout as exc:
        raise ShopeeError("A Shopee demorou demais para responder.",
                          retryable=True) from exc
    except requests.RequestException as exc:
        raise ShopeeError("Não foi possível falar com a Shopee agora.",
                          retryable=True) from exc

    if resposta.status_code == 429:
        raise ShopeeError(_mensagem_publica(RATE_LIMITED, ""),
                          retryable=True, code=RATE_LIMITED)
    if resposta.status_code >= 500:
        raise ShopeeError("A Shopee está instável no momento.", retryable=True)
    try:
        dados = resposta.json()
    except ValueError as exc:
        raise ShopeeError("A Shopee devolveu uma resposta ilegível.",
                          retryable=True) from exc

    erros = dados.get("errors") or []
    if erros:
        primeiro = erros[0] if isinstance(erros[0], dict) else {}
        code = int(primeiro.get("code") or primeiro.get("extensions", {}).get("code") or 0)
        texto = str(primeiro.get("message") or "")
        # O texto cru pode citar a query inteira; só o código e a classificação
        # entram no log, e nada dele chega ao usuário sem tradução.
        logger.warning("Shopee recusou a consulta (code=%s).", code or "?")
        raise ShopeeError(
            _mensagem_publica(code, texto),
            retryable=code == RATE_LIMITED,
            code=code,
        )
    if resposta.status_code >= 400:
        raise ShopeeError("A Shopee recusou a consulta.", code=resposta.status_code)
    return dados.get("data") or {}


# ── Consultas ────────────────────────────────────────────────────────────────
# O conjunto de campos abaixo é o núcleo documentado. Ele fica em constantes, e não
# embutido nas funções, porque a primeira coisa que muda quando a Shopee evolui o
# schema é a lista de campos — e um campo inexistente derruba a query inteira, não
# só aquele valor.

CAMPOS_PRODUTO = """
    itemId
    productName
    productLink
    offerLink
    imageUrl
    priceMin
    priceMax
    priceDiscountRate
    commissionRate
    sales
    ratingStar
    shopId
    shopName
"""

CAMPOS_CAMPANHA = """
    offerName
    offerLink
    imageUrl
    offerType
    commissionRate
    periodStartTime
    periodEndTime
"""

QUERY_PRODUTOS = """
query ProdutosShopee($keyword: String, $page: Int, $limit: Int, $listType: Int, $sortType: Int) {
  productOfferV2(keyword: $keyword, page: $page, limit: $limit, listType: $listType, sortType: $sortType) {
    nodes { %s }
    pageInfo { page limit hasNextPage }
  }
}
""" % CAMPOS_PRODUTO

QUERY_CAMPANHAS = """
query CampanhasShopee($keyword: String, $page: Int, $limit: Int, $sortType: Int) {
  shopeeOfferV2(keyword: $keyword, page: $page, limit: $limit, sortType: $sortType) {
    nodes { %s }
    pageInfo { page limit hasNextPage }
  }
}
""" % CAMPOS_CAMPANHA

QUERY_LINK = """
mutation LinkShopee($input: GenerateShortLinkInput!) {
  generateShortLink(input: $input) { shortLink }
}
"""

QUERY_CONVERSOES = """
query ConversoesShopee($inicio: Int!, $fim: Int!, $page: Int, $limit: Int) {
  conversionReport(purchaseTimeStart: $inicio, purchaseTimeEnd: $fim, page: $page, limit: $limit) {
    nodes { conversionId purchaseTime totalCommission orders utmContent }
    pageInfo { page limit hasNextPage }
  }
}
"""


def _paginar(query, chave, *, app_id, secret, variables=None, limite_paginas=MAX_PAGES):
    """Percorre páginas até acabar, com teto explícito.

    O teto existe para que uma resposta que sempre diga ``hasNextPage: true`` não
    prenda o worker indefinidamente. Quando ele é atingido a coleta é PARCIAL, e
    quem chama precisa saber disso — por isso o retorno diz se ficou completa.
    """
    linhas = []
    pagina = 1
    completa = True
    while pagina <= limite_paginas:
        variaveis = dict(variables or {})
        variaveis.update({"page": pagina, "limit": PAGE_SIZE})
        dados = executar(query, variaveis, app_id=app_id, secret=secret)
        bloco = (dados or {}).get(chave) or {}
        nos = [n for n in (bloco.get("nodes") or []) if isinstance(n, dict)]
        linhas.extend(nos)
        info = bloco.get("pageInfo") or {}
        if not nos or not info.get("hasNextPage"):
            break
        pagina += 1
    else:
        completa = False
        logger.warning(
            "Shopee: teto de %s páginas atingido em %s; coleta parcial.",
            limite_paginas, chave,
        )
    return linhas, completa


def listar_produtos(*, app_id, secret, keyword="", list_type=None, sort_type=None):
    """Ofertas de produto. ``list_type``/``sort_type`` seguem o enum da Shopee."""
    variaveis = {"keyword": keyword or None}
    if list_type is not None:
        variaveis["listType"] = int(list_type)
    if sort_type is not None:
        variaveis["sortType"] = int(sort_type)
    return _paginar(QUERY_PRODUTOS, "productOfferV2",
                    app_id=app_id, secret=secret, variables=variaveis)


def listar_campanhas(*, app_id, secret, keyword="", sort_type=None):
    """Campanhas da Shopee — o mais próximo de "cupom" que a API expõe.

    Não existe endpoint de voucher na API de afiliados. As campanhas trazem nome,
    janela de vigência e o link comissionado, que é exatamente o que o aviso de
    cupom precisa para anunciar sem prometer um código que ninguém consegue digitar.
    """
    variaveis = {"keyword": keyword or None}
    if sort_type is not None:
        variaveis["sortType"] = int(sort_type)
    return _paginar(QUERY_CAMPANHAS, "shopeeOfferV2",
                    app_id=app_id, secret=secret, variables=variaveis)


def gerar_link(url_destino, *, app_id, secret, sub_ids=None):
    """Link comissionado por uma chamada HTTP — sem navegador.

    ``sub_ids`` é o rastreio por origem. O Spreading manda o identificador do
    usuário e do canal ali, então a atribuição por cliente sai de graça, sem
    precisar de uma conta separada por tenant.
    """
    dados = executar(
        QUERY_LINK,
        {"input": {"originUrl": url_destino, "subIds": list(sub_ids or [])[:5]}},
        app_id=app_id, secret=secret,
    )
    link = ((dados or {}).get("generateShortLink") or {}).get("shortLink") or ""
    link = str(link).strip()
    if not link:
        raise ShopeeError("A Shopee não devolveu um link para este destino.")
    return link


def listar_conversoes(inicio_ts, fim_ts, *, app_id, secret):
    """Conversões do período — substitui a raspagem de relatório por API."""
    return _paginar(
        QUERY_CONVERSOES, "conversionReport",
        app_id=app_id, secret=secret,
        variables={"inicio": int(inicio_ts), "fim": int(fim_ts)},
    )


def validar_credenciais(app_id, secret):
    """Consulta barata só para provar que a credencial funciona.

    A tela de conexão chama isto ANTES de gravar: credencial errada tem de falhar na
    hora, com o usuário olhando, e não seis horas depois num worker silencioso.
    """
    executar(
        QUERY_CAMPANHAS,
        {"keyword": None, "page": 1, "limit": 1},
        app_id=app_id, secret=secret,
    )
    return True


def credenciais_da_integracao(integracao):
    """(app_id, secret) de uma IntegracaoAfiliado, com fallback para settings.

    O fallback existe só para desenvolvimento e para a conta do próprio app; em
    produção cada organização conecta a sua, e é a dela que assina.
    """
    app_id = str(getattr(integracao, "identificador_conta", "") or "").strip()
    secret = str(getattr(integracao, "token", "") or "").strip()
    if not app_id or not secret:
        app_id = str(getattr(settings, "SHOPEE_APP_ID", "") or "").strip()
        secret = str(getattr(settings, "SHOPEE_APP_SECRET", "") or "").strip()
    if not app_id or not secret:
        raise ShopeeConfigError("Conecte a conta de afiliado da Shopee.")
    return app_id, secret
