"""Amazon BR: ofertas pela página pública de busca, com o link `?tag=` do dono.

Não há API utilizável (a Creators API exige 10 vendas qualificadas em 30 dias e a credencial da
conta devolve 403), então a coleta lê o HTML da busca, uma consulta por termo no máximo uma vez por
hora. O "De" é o preço riscado que o próprio anúncio mostra, então é prova de verdade. Bloqueio
(captcha, 503) pausa a loja por uma hora: nada de insistir. Raspar viola os termos da Amazon; é
decisão do dono, e `AMAZON_COLLECT=0` desliga.
"""
import logging
import re
import time
from decimal import ROUND_HALF_UP, Decimal
from html import unescape

import requests
from django.conf import settings
from django.utils import timezone

from apps.compose.image import safe_image_url
from apps.coupons.feed import BRT

from . import niches
from .catalog import REFRESH_EVERY, search_terms
from .feeds import is_prohibited
from .models import Offer, Rule, TermRun

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.amazon.com.br/s"
SOURCE = "amazon-busca"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}
BLOCK_KEY = "amazon:blocked"
MIN_DISCOUNT, MAX_DISCOUNT = Decimal(5), Decimal(60)  # o preço de lista costuma ser inflado
MIN_RATING = Decimal("4.0")
PACE_SECONDS = 3
CARD = re.compile(r'(?=<div[^>]+data-component-type="s-search-result")')
ASIN = re.compile(r'data-asin="([A-Z0-9]{10})"')
COUPON_FINAL = re.compile(r"s-coupon-unclipped.*?Voc\S{1,3} paga\s+R\$\s*([\d.]+,\d{2})", re.S)
COUPON_VALUE = re.compile(
    r"s-coupon-clipped.*?Cupom de\s+(?:(\d+(?:,\d+)?)\s*%|R\$\s*([\d.]+(?:,\d{2})?))", re.S)
SIZE_SUFFIX = re.compile(r"\._[A-Za-z0-9_,]+_(\.(?:jpg|jpeg|png|webp))$")
MONEY = re.compile(r"(\d{1,3}(?:\.\d{3})*|\d+),(\d{2})")


class AmazonBlocked(Exception):
    pass


def enabled() -> bool:
    return bool(getattr(settings, "AMAZON_COLLECT", True))


def _money(fragment: str | None) -> Decimal | None:
    match = MONEY.search(fragment or "")
    if not match:
        return None
    return Decimal(match.group(1).replace(".", "") + "." + match.group(2))


def _first(pattern: str, block: str, flags=re.S):
    match = re.search(pattern, block, flags)
    return match.group(1) if match else None


def parse(html: str) -> list[dict]:
    """Cartões da busca: asin, título, foto, preço atual, preço riscado e nota."""
    out = []
    for block in CARD.split(html)[1:]:
        block = block[:16000]
        asin = _first(ASIN.pattern, block)
        title = re.sub(r"<[^>]+>", "", _first(r"<h2[^>]*>(.*?)</h2>", block) or "").strip()
        price = _money(_first(r'<span class="a-price"[^>]*>.*?a-offscreen">([^<]+)<', block))
        struck = _money(_first(r'a-price a-text-price[^>]*>.*?a-offscreen">([^<]+)<', block))
        rating = _first(r'a-icon-alt">(\d,\d) de 5', block)
        image = _first(r'class="s-image"[^>]*?src="([^"]+)"', block)
        coupon_final = _money(_first(COUPON_FINAL.pattern, block))
        value = COUPON_VALUE.search(block)
        coupon_pct = Decimal(value.group(1).replace(",", ".")) if value and value.group(1) else None
        coupon_amount = (
            Decimal(value.group(2).replace(".", "").replace(",", ".")) if value and value.group(2)
            else None)
        if asin and title and price and (struck or coupon_final) and image:
            # `._AC_UL320_.jpg` é a miniatura de 320 px; sem o sufixo vem a imagem original.
            out.append({"asin": asin, "title": unescape(title),
                        "image": SIZE_SUFFIX.sub(r"\g<1>", unescape(image)),
                        "price": price, "struck": struck, "coupon_final": coupon_final,
                        "coupon_pct": coupon_pct, "coupon_amount": coupon_amount,
                        "rating": Decimal(rating.replace(",", ".")) if rating else None})
    return out


def vet(card: dict) -> dict | None:
    """Preço final = o que o cartão mostra com o cupom ("Você paga R$ x com o cupom") ou o preço
    atual. A base do "De" é o preço riscado do anúncio ou, sem ele, o preço antes do cupom."""
    price, struck = card["price"], card["struck"]
    final = card.get("coupon_final")
    if final and final >= price:
        final = None
    if card["rating"] is None or card["rating"] < MIN_RATING:
        return None
    base = struck if struck and struck > price else (price if final else None)
    target = final or price
    if base is None or base <= target:
        return None
    pct = ((1 - target / base) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    image = safe_image_url(card["image"])
    if not (MIN_DISCOUNT <= pct <= MAX_DISCOUNT) or not image or is_prohibited(card["title"]):
        return None
    return {**card, "pct": pct, "image": image, "final": target, "base": base,
            "coupon": bool(final)}


def fetch(term: str, http=None) -> str:
    try:
        response = (http or requests).get(
            SEARCH_URL, params={"k": term}, headers=HEADERS, timeout=20)
    except requests.exceptions.RequestException as exc:
        raise AmazonBlocked(f"rede: {type(exc).__name__}") from exc
    body = response.text or ""
    if response.status_code != 200 or "captcha" in body.lower()[:20000]:
        raise AmazonBlocked(f"http {response.status_code}")
    return body


def collect(now=None, http=None, sleep=time.sleep) -> int:
    if not enabled():
        return 0
    now = now or timezone.now()
    paused = TermRun.objects.filter(term=BLOCK_KEY).values_list("last_run", flat=True).first()
    if paused and now - paused < REFRESH_EVERY:
        return 0
    today = now.astimezone(BRT).date()
    stored: set = set()
    first = True
    for rule in Rule.unscoped.filter(active=True, type=Rule.OFFERS).select_related("org"):
        if "amazon" not in rule.store_list or not rule.org.amazon_tag:
            continue
        for term in search_terms(rule):
            key = f"amazon:{term}"[:120]
            last = TermRun.objects.filter(term=key).values_list("last_run", flat=True).first()
            if last and now - last < REFRESH_EVERY:
                continue
            if not first:
                sleep(PACE_SECONDS)
            first = False
            try:
                html = fetch(term, http)
            except AmazonBlocked as exc:
                logger.warning("amazon bloqueou a coleta (%s); loja pausada por 1 h", exc)
                TermRun.objects.update_or_create(term=BLOCK_KEY, defaults={"last_run": now})
                return len(stored)
            for card in parse(html):
                row = vet(card)
                if row and row["asin"] not in stored:
                    stored.add(row["asin"])
                    Offer.objects.update_or_create(
                        store="amazon", link_key=row["asin"], valid_on=today,
                        defaults={
                            "source": SOURCE, "product_key": row["asin"],
                            "source_url": f"https://www.amazon.com.br/dp/{row['asin']}",
                            "title": row["title"][:300],
                            "url": f"https://www.amazon.com.br/dp/{row['asin']}",
                            "image_url": row["image"], "macro": niches.classify(row["title"]),
                            "price_from": row["base"], "price_to": row["final"],
                            "discount_pct": row["pct"], "last_seen": now,
                            # cupom de ativação na página do produto; sem prazo no cartão, vale o
                            # frescor (3 h) e a coleta de hora em hora
                            "coupon_kind": "activation" if row["coupon"] else "",
                            "coupon_pct": row["coupon_pct"] if row["coupon"] else None,
                            "coupon_amount": row["coupon_amount"] if row["coupon"] else None,
                            "coupon_cap": None, "coupon_min": None, "coupon_until": None,
                        },
                    )
            TermRun.objects.update_or_create(term=key, defaults={"last_run": now})
    return len(stored)
