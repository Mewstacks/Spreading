"""Cupons da página oficial de ofertas da Amazon BR (coleção "cupons"), lidos do HTML cru.

A página embute, por deal, um JSON com ASIN, título, foto, preço, preço de lista e o bloco
`coupon` com "Você paga R$ X com o cupom": par produto+cupom com o preço final provado pela
própria Amazon, sem navegador. Mesmo regime da busca (`amazon.py`): uma carga por hora, bloqueio
pausa a loja por uma hora, `AMAZON_COLLECT=0` desliga. Raspar viola os termos da Amazon; a decisão
é do dono.
"""
import json
import logging
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import requests
from django.utils import timezone

from apps.compose.image import safe_image_url
from apps.coupons.feed import BRT

from . import niches
from .amazon import BLOCK_KEY, HEADERS, MAX_DISCOUNT, MIN_DISCOUNT, MIN_RATING, enabled
from .catalog import REFRESH_EVERY
from .feeds import is_prohibited
from .models import Offer, Rule, TermRun

logger = logging.getLogger(__name__)

URL = "https://www.amazon.com.br/deals?bubble-id=deals-collection-coupons"
SOURCE = "amazon-cupons"
RUN_KEY = "amazon:cupons"
MIN_REVIEWS = 10
DEAL = re.compile(r'\{\s*"asin"\s*:\s*"([A-Z0-9]{10})"\s*,\s*"title"')


class DealsBlocked(Exception):
    pass


def fetch(http=None) -> str:
    client = http or requests.Session()
    try:
        response = client.get(URL, headers=HEADERS, timeout=30, allow_redirects=True)
    except requests.exceptions.RequestException as exc:
        raise DealsBlocked(f"rede: {type(exc).__name__}") from exc
    body = response.text or ""
    if response.status_code != 200 or "captcha" in body.lower()[:20000]:
        raise DealsBlocked(f"http {response.status_code}")
    return body


def _dec(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if number > 0 else None


def _coupon_final(coupon: dict) -> Decimal | None:
    for fragment in (coupon.get("label") or {}).get("fragments") or []:
        if isinstance(fragment, dict) and "money" in fragment:
            return _dec((fragment["money"] or {}).get("amount"))
    return None


def parse(html: str) -> list[dict]:
    """Deals com cupom: asin, título, foto, preço antes do cupom, preço de lista, final, nota."""
    decoder, out, seen = json.JSONDecoder(), [], set()
    for match in DEAL.finditer(html):
        try:
            deal, _end = decoder.raw_decode(html, match.start())
        except ValueError:
            continue
        coupon = deal.get("coupon") or {}
        final = _coupon_final(coupon) if isinstance(coupon, dict) else None
        if not final or deal["asin"] in seen:
            continue
        price = deal.get("price") or {}
        to_pay = _dec((price.get("priceToPay") or {}).get("price"))
        basis = _dec((price.get("basisPrice") or {}).get("price"))
        hi = ((deal.get("image") or {}).get("hiRes") or {})
        image = f"{hi.get('baseUrl', '')}.{hi.get('extension', 'jpg')}" if hi.get("baseUrl") else ""
        reviews = deal.get("customerReviews") or {}
        rating = (reviews.get("rating") or {}).get("shortDisplayString") or ""
        stars = Decimal(rating.replace(",", ".")) if re.fullmatch(r"\d(?:,\d)?", rating) else None
        seen.add(deal["asin"])
        out.append({
            "asin": deal["asin"], "title": str(deal.get("title") or "").strip(), "image": image,
            "price": to_pay, "basis": basis, "final": final,
            "rating": stars,
            "reviews": int(((reviews.get("count") or {}).get("value")) or 0),
        })
    return out


def vet(deal: dict) -> dict | None:
    price, final = deal["price"], deal["final"]
    if not price or final >= price or not deal["title"]:
        return None
    if deal["rating"] is None or deal["rating"] < MIN_RATING or deal["reviews"] < MIN_REVIEWS:
        return None
    base = deal["basis"] if deal["basis"] and deal["basis"] > price else price
    pct = ((1 - final / base) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    image = safe_image_url(deal["image"])
    if not (MIN_DISCOUNT <= pct <= MAX_DISCOUNT) or not image or is_prohibited(deal["title"]):
        return None
    coupon_pct = ((1 - final / price) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return {**deal, "base": base, "pct": pct, "image": image, "coupon_pct": coupon_pct}


def collect(now=None, http=None) -> int:
    """Uma carga por hora quando há regra ativa com Amazon e a tag da organização."""
    if not enabled():
        return 0
    now = now or timezone.now()
    paused = TermRun.objects.filter(term=BLOCK_KEY).values_list("last_run", flat=True).first()
    last = TermRun.objects.filter(term=RUN_KEY).values_list("last_run", flat=True).first()
    if (paused and now - paused < REFRESH_EVERY) or (last and now - last < REFRESH_EVERY):
        return 0
    wanted = any(
        "amazon" in rule.store_list and rule.org.amazon_tag
        for rule in Rule.unscoped.filter(active=True, type=Rule.OFFERS).select_related("org")
    )
    if not wanted:
        return 0
    try:
        html = fetch(http)
    except DealsBlocked as exc:
        logger.warning("amazon (cupons) bloqueou (%s); loja pausada por 1 h", exc)
        TermRun.objects.update_or_create(term=BLOCK_KEY, defaults={"last_run": now})
        return 0
    today, count = now.astimezone(BRT).date(), 0
    for deal in parse(html):
        row = vet(deal)
        if not row:
            continue
        url = f"https://www.amazon.com.br/dp/{row['asin']}"
        Offer.objects.update_or_create(
            store="amazon", link_key=row["asin"], valid_on=today,
            defaults={
                "source": SOURCE, "product_key": row["asin"], "source_url": url,
                "title": row["title"][:300], "url": url, "image_url": row["image"],
                "macro": niches.classify(row["title"]), "price_from": row["base"],
                "price_to": row["final"], "discount_pct": row["pct"], "last_seen": now,
                "coupon_kind": "activation", "coupon_pct": row["coupon_pct"],
                "coupon_amount": None, "coupon_cap": None, "coupon_min": None,
                "coupon_until": None,
            },
        )
        count += 1
    TermRun.objects.update_or_create(term=RUN_KEY, defaults={"last_run": now})
    return count
