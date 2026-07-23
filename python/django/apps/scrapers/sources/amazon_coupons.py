"""Cupons públicos da página oficial de Ofertas da Amazon Brasil.

A Creators API é a fonte de catálogo/preço para ofertas comuns, mas o recurso
OffersV2 não publica o desconto dos cupons de ativação. A página oficial de
Ofertas possui um filtro próprio de cupons e expõe, por card, ASIN, identificador
da promoção, preço atual e preço efetivo depois de ativar o cupom.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit

from django.utils import timezone

from apps.scrapers.auxiliar import iniciar_browser
from apps.scrapers.coupon_rules import normalizar_regras_cupom

from .base import IngestedItem, SourceAdapter


COUPONS_URL = (
    "https://www.amazon.com.br/deals"
    "?bubble-id=deals-collection-coupons"
)
_PROMO_RE = re.compile(r"(?:^|:)amzn1\.coupon\./promo/([^:]+)")
_FINAL_RE = re.compile(
    r"Você paga\s+R\$\s*([\d.\s]+(?:,\d{1,2})?)\s+com o cupom", re.I
)
_MONEY_RE = re.compile(r"R\$\s*([\d.\s]+(?:,\d{1,2})?)", re.I)


def _money(texto):
    match = _MONEY_RE.search((texto or "").replace("\xa0", " "))
    if not match:
        return 0.0
    raw = match.group(1).replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return round(float(raw), 2)
    except ValueError:
        return 0.0


def _canonical_product_url(url, asin):
    try:
        parsed = urlsplit(url or "")
    except ValueError:
        parsed = None
    if parsed and parsed.scheme in {"http", "https"} and parsed.netloc.endswith("amazon.com.br"):
        return urlunsplit(("https", "www.amazon.com.br", parsed.path, "", ""))
    return f"https://www.amazon.com.br/dp/{asin}"


class AmazonCouponsSource(SourceAdapter):
    slug = "amazon-public-coupons"
    marketplace = "amazon"
    name = "Amazon — cupons oficiais"

    def __init__(self):
        self._cache = []
        self._cache_at = 0.0

    def _snapshot(self):
        # run_source chama discover_offers e discover_coupons em sequência. Reusar o
        # mesmo DOM evita abrir a Amazon duas vezes e reduz a chance de bloqueio.
        if self._cache and time.monotonic() - self._cache_at < 120:
            return list(self._cache)

        rows = []
        with iniciar_browser(headless=True, validar_sessao=False) as (page, _):
            page.goto(COUPONS_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1800)
            body = page.locator("body").inner_text(timeout=10000)
            if "digite os caracteres" in body.casefold():
                raise RuntimeError("captcha")
            cards = page.locator("[data-testid='product-card'][data-asin]")
            for index in range(cards.count()):
                card = cards.nth(index)
                try:
                    asin = (card.get_attribute("data-asin") or "").strip().upper()
                    item_id = card.get_attribute("data-csa-c-item-id") or ""
                    promo_match = _PROMO_RE.search(item_id)
                    if not asin or not promo_match:
                        continue
                    promo_id = promo_match.group(1)
                    text = card.inner_text(timeout=3000)
                    final_match = _FINAL_RE.search(text.replace("\xa0", " "))
                    final = _money(f"R$ {final_match.group(1)}") if final_match else 0
                    price = card.locator(
                        "[data-testid='price-section'] .a-price .a-offscreen"
                    )
                    current = _money(price.first.inner_text(timeout=1500)) if price.count() else 0
                    previous = card.locator(
                        "[data-testid='price-section'] .a-price.a-text-price .a-offscreen"
                    )
                    reference = (
                        _money(previous.first.inner_text(timeout=1000))
                        if previous.count() else current
                    )
                    title = ""
                    full = card.locator(".a-truncate-full")
                    if full.count():
                        title = full.first.inner_text(timeout=1500).strip()
                    image = card.locator("img")
                    image_url = image.first.get_attribute("src") if image.count() else ""
                    if not title and image.count():
                        title = (image.first.get_attribute("alt") or "").strip()
                    link = card.locator("a[data-testid='product-card-link']")
                    href = link.first.get_attribute("href") if link.count() else ""
                    if not title or not image_url or current <= 0 or final <= 0 or final >= current:
                        continue
                    discount = round((current - final) * 100 / current, 2)
                    if discount <= 0 or discount >= 90:
                        continue
                    rows.append({
                        "asin": asin,
                        "promo_id": promo_id,
                        "title": title[:255],
                        "url": _canonical_product_url(href, asin),
                        "image_url": image_url.split("?", 1)[0][:1000],
                        "current": current,
                        "reference": reference if reference >= current else current,
                        "final": final,
                        "discount": discount,
                    })
                except Exception:
                    continue

        self._cache = rows
        self._cache_at = time.monotonic()
        return list(rows)

    def discover_offers(self, **kwargs):
        observed = timezone.now()
        for row in self._snapshot():
            yield IngestedItem(
                external_id=row["asin"],
                marketplace=self.marketplace,
                source=self.slug,
                kind="offer",
                canonical_url=row["url"],
                title=row["title"],
                current_price=row["current"],
                reference_price=row["reference"],
                image_url=row["image_url"],
                observed_at=observed,
                evidence={
                    "transport": "amazon-official-deals",
                    "association": "amazon-official-coupon-page",
                    "coupon_final_price": row["final"],
                    "promotion": {
                        "present": True,
                        "coupon_confirmed": True,
                        "id": row["promo_id"],
                        "label": f"{row['discount']:g}% off",
                    },
                },
            )

    def discover_coupons(self, **kwargs):
        observed = timezone.now()
        groups = defaultdict(list)
        for row in self._snapshot():
            groups[row["promo_id"]].append(row)
        for promo_id, rows in groups.items():
            discounts = [row["discount"] for row in rows]
            discount = min(discounts)
            title = (
                f"Cupom Amazon — {discount:g}% OFF"
                if max(discounts) - min(discounts) < 0.01
                else f"Cupom Amazon — até {max(discounts):g}% OFF"
            )
            yield IngestedItem(
                external_id=f"amazon-coupon:{promo_id}"[:160],
                marketplace=self.marketplace,
                source=self.slug,
                kind="coupon",
                canonical_url=COUPONS_URL,
                title=title,
                coupon_rules=normalizar_regras_cupom({
                    "tipo_desconto": "porcentagem",
                    "valor_desconto": discount,
                    "modo_resgate": "ativacao",
                    "escopo": "produtos selecionados",
                }, external_id=f"amazon-coupon:{promo_id}"),
                content_type="promotion",
                observed_at=observed,
                evidence={
                    "transport": "amazon-official-deals",
                    "association": "amazon-official-coupon-page",
                    "promotion_id": promo_id,
                    "asins": [row["asin"] for row in rows],
                },
            )

    def healthcheck(self):
        try:
            return {"ok": bool(self._snapshot())}
        except Exception as exc:
            return {"ok": False, "erro": str(exc)}
