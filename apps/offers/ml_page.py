"""Vitrine pública de cupons do Mercado Livre (`/ofertas/cupons`): campanhas e ofertas em destaque.

A página responde 200 sem login e embute o estado dos blocos (`__NORDIC_RENDERING_CTX__`):
- `coupons-carousel`: cupons de ativação ativos, com valor, mínimo, teto, validade, categoria e o
  container onde ficam os produtos (host que o ML mura: aqui só vira link para o dono abrir);
- `products-gallery`: ofertas em destaque, com preço atual, preço anterior e foto.

Uma carga por hora, e uma falha também espera uma hora. O programa de afiliados proíbe extração
automatizada do site (cláusula 4): `ML_PUBLIC_PAGES=0` desliga, e manter ligado é decisão do dono.
Preço condicionado ao meio de pagamento ("no Pix") não vira oferta: a mensagem não diz a condição.
"""
import json
import logging
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.compose.image import safe_image_url
from apps.coupons.feed import BRT, safe_container_url
from apps.coupons.models import CampaignCoupon

from . import niches
from .catalog import REFRESH_EVERY
from .feeds import is_prohibited
from .keys import product_key
from .models import Offer, TermRun

logger = logging.getLogger(__name__)

URL = "https://www.mercadolivre.com.br/ofertas/cupons"
SOURCE = "ml-ofertas-cupons"
RUN_KEY = "ml:pagina-cupons"
WALL = "account-verification"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}
CTX = re.compile(r'<script id="__NORDIC_RENDERING_CTX__"[^>]*>(.*?)</script>', re.S)
MONEY = re.compile(r"R\$\s*([\d.]+(?:,\d{1,2})?)")
PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
CATALOG_ID = re.compile(r"^MLB\d+$")
MAX_DEPTH = 12


def enabled() -> bool:
    return bool(getattr(settings, "ML_PUBLIC_PAGES", True))


def _money(text) -> Decimal | None:
    match = MONEY.search(str(text or ""))
    if not match:
        return None
    return Decimal(match.group(1).replace(".", "").replace(",", ".")).quantize(Decimal("0.01"))


def _price(node) -> Decimal | None:
    try:
        number = Decimal(str(node))
    except (InvalidOperation, ValueError):
        return None
    return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if number > 0 else None


def _states(node, depth=0):
    if depth > MAX_DEPTH or not isinstance(node, dict):
        return
    for key, value in node.items():
        if key.endswith("FLOX_STATE") and isinstance(value, dict):
            yield value
        elif isinstance(value, dict):
            yield from _states(value, depth + 1)


def bricks(html: str) -> list[dict]:
    """Blocos da página (`brickStack`); [] se o contrato mudou."""
    match = CTX.search(html or "")
    if not match or "{" not in match.group(1):
        return []
    body = match.group(1)
    try:
        ctx, _end = json.JSONDecoder().raw_decode(body, body.index("{"))
    except (ValueError, RecursionError):
        return []
    found = []
    for state in _states(ctx):
        stack = state.get("brickStack")
        if isinstance(stack, dict):
            found.extend(b for b in stack.values() if isinstance(b, dict))
    return found


def _rows(brick: dict, *path) -> list[dict]:
    node = brick.get("data") or {}
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return [row for row in node or [] if isinstance(row, dict)] if isinstance(node, list) else []


def parse_coupons(html: str, now=None) -> list[dict]:
    now = now or timezone.now()
    out = []
    for brick in bricks(html):
        if brick.get("ui_type") != "coupons-carousel":
            continue
        for row in _rows(brick, "coupons"):
            if (row.get("status") or {}).get("id") != "ACTIVE" or not row.get("campaign_id"):
                continue
            expires = parse_datetime(str(row.get("expiration_date") or ""))
            if expires and expires <= now:
                continue
            title = str((row.get("title") or {}).get("text") or "").strip()
            mode = str(row.get("benefit_mode") or "").upper()
            percent = PERCENT.search(title)
            pct = amount = None
            if mode == "PERCENT" or (not mode and percent):
                pct = Decimal(percent.group(1).replace(",", ".")) if percent else None
            else:
                amount = _money(title)
            if pct is None and amount is None:
                continue  # sem valor legível, não é cupom para mostrar
            money = row.get("amount") or {}
            link = str((row.get("action") or {}).get("value") or "").split("#")[0]
            out.append({
                "campaign_id": str(row["campaign_id"])[:20], "title": title[:60],
                "category": str(row.get("category") or "")[:80], "mode": mode[:8],
                "pct": pct, "amount": amount,
                "min": _money(money.get("min_amount")), "cap": _money(money.get("cap_amount")),
                "expires": expires, "container": safe_container_url(link),
                "items": int((row.get("segmentations") or {}).get("total_items") or 0),
            })
    return out


def _part(card: dict, kind: str) -> dict:
    return next((p for p in card.get("components") or [] if p.get("type") == kind), {})


def parse_offers(html: str) -> list[dict]:
    """Cartões da galeria que viram oferta: produto de catálogo, preço à vista, "De" e foto."""
    out = []
    for brick in bricks(html):
        if brick.get("ui_type") != "products-gallery":
            continue
        result = (((brick.get("data") or {}).get("items") or {}).get("result")) or {}
        for card in result.get("polycards") or []:
            if not isinstance(card, dict):
                continue
            meta = card.get("metadata") or {}
            product = str(meta.get("product_id") or "")
            link = f"https://{meta.get('url')}" if meta.get("url") else ""
            title = str(((_part(card, "title").get("title")) or {}).get("text") or "").strip()
            price = _part(card, "price").get("price") or {}
            promotions = ((meta.get("tracks") or {}).get("price") or {}).get("promotions") or []
            conditional = bool((price.get("unit_description") or {}).get("text")) or any(
                c.get("type") == "PIX" for p in promotions for c in p.get("campaigns") or [])
            now = _price(((meta.get("tracks") or {}).get("price") or {}).get("price")
                         or (price.get("current_price") or {}).get("value"))
            before = None
            for label in price.get("price_labels") or []:
                for value in label.get("values") or []:
                    if value.get("key") == "previous_price":
                        before = _price((value.get("price") or {}).get("value"))
            picture = next((p.get("id") for p in (card.get("pictures") or {}).get("pictures") or []
                            if p.get("id")), "")
            host = (urlparse(link).hostname or "").lower()
            if (CATALOG_ID.match(product) and product_key(link) == product and title and picture
                    and host.endswith("mercadolivre.com.br") and not conditional
                    and now and before and before > now):
                out.append({"product": product, "title": title, "price": now, "before": before,
                            "image": f"https://http2.mlstatic.com/D_NQ_NP_{picture}-F.webp"})
    return out


def _body(response) -> str:
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")  # `.text` chuta ISO-8859-1 sem charset
    return response.text or ""


def fetch(http=None) -> str:
    response = (http or requests).get(
        URL, headers=HEADERS, timeout=settings.HTTP_TIMEOUT_SECONDS, allow_redirects=True)
    if response.status_code != 200 or WALL in str(getattr(response, "url", "")):
        raise requests.exceptions.HTTPError(f"http {response.status_code}")
    return _body(response)


def collect(now=None, http=None) -> dict:
    """Uma carga por hora: grava campanhas de cupom e ofertas em destaque. {} se nada a fazer."""
    if not enabled():
        return {}
    now = now or timezone.now()
    last = TermRun.objects.filter(term=RUN_KEY).values_list("last_run", flat=True).first()
    if last and now - last < REFRESH_EVERY:
        return {}
    TermRun.objects.update_or_create(term=RUN_KEY, defaults={"last_run": now})  # falha espera 1 h
    try:
        html = fetch(http)
    except requests.exceptions.RequestException as exc:
        logger.warning("ml (vitrine de cupons) falhou: %s", exc)
        return {}
    coupons, offers = parse_coupons(html, now), parse_offers(html)
    if not coupons and not offers:
        logger.warning("ml (vitrine de cupons): página sem blocos conhecidos, contrato mudou?")
    for row in coupons:
        CampaignCoupon.objects.update_or_create(
            campaign_id=row["campaign_id"],
            defaults={"title": row["title"], "category": row["category"], "mode": row["mode"],
                      "discount_pct": row["pct"], "discount_amount": row["amount"],
                      "min_purchase": row["min"], "max_discount": row["cap"],
                      "expires_at": row["expires"], "container_url": row["container"],
                      "total_items": row["items"], "last_seen": now},
        )
    today, stored = now.astimezone(BRT).date(), 0
    for row in offers:
        image = safe_image_url(row["image"])
        if not image or is_prohibited(row["title"]):
            continue
        known = Offer.objects.filter(
            store="mercadolivre", link_key=row["product"], valid_on=today).first()
        if known and known.source != SOURCE:
            continue  # a API ou o feed do dia já tem este produto hoje: não rebaixar a prova
        pct = ((1 - row["price"] / row["before"]) * 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        url = f"https://www.mercadolivre.com.br/p/{row['product']}"
        Offer.objects.update_or_create(
            store="mercadolivre", link_key=row["product"], valid_on=today,
            defaults={
                "source": SOURCE, "product_key": row["product"], "source_url": url,
                "title": row["title"][:300], "url": url, "image_url": image,
                "macro": niches.classify(row["title"]), "price_from": row["before"],
                "price_to": row["price"], "discount_pct": pct, "last_seen": now,
            },
        )
        stored += 1
    return {"campanhas": len(coupons), "ofertas": stored}
