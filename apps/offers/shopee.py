"""Shopee Affiliate Open API (GraphQL assinado): ofertas já com o link comissionado do dono.

Sem navegador e sem Link Builder: `productOfferV2` devolve `offerLink`, o link de afiliado de cada
produto. A API não traz o preço original, só `priceDiscountRate`; o "De" é derivado dela (arredonda
no selo da Shopee, então pode errar centavos) e por isso só entra produto com desconto plausível,
nota e vendas de quem existe de verdade. Falha da fonte nunca apaga catálogo: a coleta devolve o
que já tinha e tenta o termo de novo no ciclo seguinte.
"""
import hashlib
import json
import logging
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.utils import timezone

from apps.compose.image import safe_image_url
from apps.coupons.feed import BRT

from . import niches
from .catalog import REFRESH_EVERY, search_terms
from .feeds import is_prohibited
from .models import AffiliateLink, Offer, Rule, TermRun

logger = logging.getLogger(__name__)

API_URL = "https://open-api.affiliate.shopee.com.br/graphql"
TIMEOUT = (5, 20)
RATE_LIMITED = 10030
PAGE_SIZE = 50
PACE_SECONDS = 1.5
SORT_BY_SALES = 2  # confirmado na API real; o padrão devolve anúncio novo sem venda nem nota
SOURCE = "shopee-api"
HOSTS = ("shopee.com.br", "shp.ee", "shope.ee")
MIN_DISCOUNT, MAX_DISCOUNT = Decimal(5), Decimal(70)  # acima disso o "De" costuma ser fachada
MIN_RATING, MIN_SALES = Decimal("4.5"), 50

FIELDS = """
    itemId productName productLink offerLink imageUrl priceMin priceMax priceDiscountRate
    commissionRate sales ratingStar shopId shopName
"""
QUERY_PRODUCTS = """
query ProdutosShopee($keyword: String, $page: Int, $limit: Int, $sortType: Int) {
  productOfferV2(keyword: $keyword, page: $page, limit: $limit, sortType: $sortType) {
    nodes { %s }
    pageInfo { page limit hasNextPage }
  }
}
""" % FIELDS


class ShopeeError(Exception):
    """Falha explicável. Nunca carrega segredo, assinatura nem o corpo cru da resposta."""

    def __init__(self, message, *, retryable=False, code=0):
        super().__init__(message)
        self.retryable, self.code = retryable, code


class ShopeeConfigError(ShopeeError):
    pass


def configured() -> bool:
    app_id = getattr(settings, "SHOPEE_APP_ID", "")
    return bool(app_id and getattr(settings, "SHOPEE_APP_SECRET", ""))


def sign(app_id: str, secret: str, payload: str, timestamp: int) -> str:
    return hashlib.sha256(f"{app_id}{timestamp}{payload}{secret}".encode()).hexdigest()


def execute(query: str, variables=None, *, app_id: str, secret: str, http=None) -> dict:
    """Query assinada; devolve `data`. `errors` do GraphQL vêm com HTTP 200: viram exceção, senão
    uma fonte quebrada passaria por coleta vazia."""
    if not app_id or not secret:
        raise ShopeeConfigError("Falta o App ID e o Secret da Shopee.")
    # Uma serialização só: a mesma string assina e viaja (reserializar invalida a assinatura).
    payload = json.dumps({"query": query, "variables": variables or {}},
                         separators=(",", ":"), ensure_ascii=False)
    stamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "Authorization": (f"SHA256 Credential={app_id}, Timestamp={stamp}, "
                          f"Signature={sign(app_id, secret, payload, stamp)}"),
    }
    try:
        response = (http or requests).post(
            API_URL, data=payload.encode("utf-8"), headers=headers, timeout=TIMEOUT
        )
    except requests.exceptions.Timeout as exc:
        raise ShopeeError("A Shopee demorou demais.", retryable=True) from exc
    except requests.exceptions.RequestException as exc:
        raise ShopeeError("Sem conexão com a Shopee.", retryable=True) from exc
    if response.status_code == 429:
        raise ShopeeError("A Shopee pediu para reduzir o ritmo.", retryable=True, code=RATE_LIMITED)
    if response.status_code >= 500:
        raise ShopeeError("A Shopee está instável.", retryable=True)
    try:
        body = response.json()
    except ValueError as exc:
        raise ShopeeError("Resposta ilegível da Shopee.", retryable=True) from exc
    errors = body.get("errors") or []
    if errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        code = int(first.get("code") or (first.get("extensions") or {}).get("code") or 0)
        logger.warning("Shopee recusou a consulta (code=%s).", code or "?")
        raise ShopeeError("A Shopee recusou a consulta.", retryable=code == RATE_LIMITED, code=code)
    if response.status_code >= 400:
        raise ShopeeError("A Shopee recusou a consulta.", code=response.status_code)
    return body.get("data") or {}


def safe_link(url) -> str:
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    ok = parsed.scheme == "https" and any(host == h or host.endswith("." + h) for h in HOSTS)
    return str(url).strip() if ok else ""


def _dec(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if number > 0 else None


def vet(node: dict) -> dict | None:
    """Campos publicáveis de um nó da API, ou None se o produto não presta para o grupo."""
    try:
        item = str(node["itemId"]).strip()
        price = _dec(node.get("priceMin"))
        if price != _dec(node.get("priceMax")):
            return None  # variações com preços diferentes: o desconto não diz de qual é o "De"
        rate = Decimal(str(node.get("priceDiscountRate") or 0))
        rating = Decimal(str(node.get("ratingStar") or 0))
        sales = int(node.get("sales") or 0)
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return None
    title = str(node.get("productName") or "").strip()
    link, image = safe_link(node.get("offerLink")), safe_image_url(node.get("imageUrl") or "")
    url = safe_link(node.get("productLink")) or link
    if not (item and title and price and link and image and url):
        return None
    if not (MIN_DISCOUNT <= rate <= MAX_DISCOUNT) or rating < MIN_RATING or sales < MIN_SALES:
        return None
    if is_prohibited(title):
        return None
    price_from = (price / (1 - rate / 100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    pct = ((1 - price / price_from) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"key": f"shp:{item}", "title": title[:300], "url": url[:600], "link": link,
            "image": image, "price_to": price, "price_from": price_from, "pct": pct,
            "commission": _commission(node.get("commissionRate"))}


def _commission(raw) -> Decimal | None:
    """`commissionRate` vem como fração ("0.07" = 7%); aceita já em % por segurança."""
    value = _dec(raw)
    if value is None:
        return None
    percent = value * 100 if value <= 1 else value
    return min(percent, Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _store(org, row: dict, now, today) -> None:
    Offer.objects.update_or_create(
        store="shopee", link_key=row["key"], valid_on=today,
        defaults={
            "source": SOURCE, "product_key": row["key"], "source_url": row["url"],
            "title": row["title"], "url": row["url"], "image_url": row["image"],
            "macro": niches.classify(row["title"]), "price_from": row["price_from"],
            "price_to": row["price_to"], "discount_pct": row["pct"],
            "commission_pct": row["commission"], "last_seen": now,
        },
    )
    AffiliateLink.unscoped.update_or_create(
        org=org, product_key=row["key"],
        defaults={"affiliate_url": row["link"], "etiqueta": SOURCE},
    )


def collect(now=None, http=None, sleep=time.sleep) -> int:
    """Uma consulta por termo das regras ativas com Shopee, no máximo uma por hora cada."""
    if not configured():
        return 0
    now = now or timezone.now()
    today = now.astimezone(BRT).date()
    app_id, secret = settings.SHOPEE_APP_ID, settings.SHOPEE_APP_SECRET
    first = True
    stored: set = set()  # o mesmo produto aparece em vários termos: conta e grava uma vez
    rules = Rule.unscoped.filter(active=True, type=Rule.OFFERS).select_related("org")
    for rule in rules:
        if "shopee" not in rule.store_list:
            continue
        for term in search_terms(rule, general=True):
            key = f"shopee:{rule.org_id}:{term}"[:120]
            last = TermRun.objects.filter(term=key).values_list("last_run", flat=True).first()
            if last and now - last < REFRESH_EVERY:
                continue
            if not first:
                sleep(PACE_SECONDS)  # a API recusa rajada de consultas
            first = False
            try:
                data = execute(QUERY_PRODUCTS, {"keyword": term or None, "page": 1,
                                                "limit": PAGE_SIZE, "sortType": SORT_BY_SALES},
                               app_id=app_id, secret=secret, http=http)
            except ShopeeError as exc:
                logger.warning("shopee: %s", exc)
                return len(stored)  # fonte ruim: para o ciclo, mantém o catálogo e tenta depois
            for node in ((data.get("productOfferV2") or {}).get("nodes") or []):
                row = vet(node) if isinstance(node, dict) else None
                if row and (rule.org_id, row["key"]) not in stored:
                    _store(rule.org, row, now, today)
                    stored.add((rule.org_id, row["key"]))
            TermRun.objects.update_or_create(term=key, defaults={"last_run": now})
    return len(stored)
