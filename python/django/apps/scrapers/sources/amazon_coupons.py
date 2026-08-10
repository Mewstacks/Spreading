"""Cupons públicos da página oficial de Ofertas da Amazon Brasil.

A Creators API é a fonte de catálogo/preço para ofertas comuns, mas o recurso
OffersV2 não publica o desconto dos cupons de ativação. A página oficial de
Ofertas possui um filtro próprio de cupons e expõe, por card, ASIN, identificador
da promoção, preço atual e preço efetivo depois de ativar o cupom.
"""
from __future__ import annotations

import re
import time
import hashlib
import logging
from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit

from django.utils import timezone
from django.conf import settings

from apps.scrapers.auxiliar import iniciar_browser
from apps.scrapers.coupon_rules import normalizar_regras_cupom
from apps.scrapers.source_diagnostics import capture_public_diagnostic

from .base import IngestedItem, SourceAdapter, normalizar_dinheiro


COUPONS_URL = (
    "https://www.amazon.com.br/deals"
    "?bubble-id=deals-collection-coupons"
)
_PROMO_RE = re.compile(r"(?:^|:)amzn1\.coupon\./promo/([^:]+)")
_FINAL_RE = re.compile(
    r"Você paga\s+R\$\s*([\d.\s]+(?:,\d{1,2})?)\s+com o cupom", re.I
)
_MONEY_RE = re.compile(r"R\$\s*([\d.\s]+(?:,\d{1,2})?)", re.I)
_ASIN_URL_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", re.I)
_PROMO_FALLBACK_RE = re.compile(
    r"(?:coupon|cupom)[^A-Za-z0-9]{0,20}(?:promo(?:tion)?[/=:])?([A-Za-z0-9._-]{4,})",
    re.I,
)
logger = logging.getLogger(__name__)


def _money(texto):
    match = _MONEY_RE.search((texto or "").replace("\xa0", " "))
    if not match:
        return 0.0
    return normalizar_dinheiro(match.group(1))


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
    requires_chromium = True

    def __init__(self):
        self._cache = []
        self._cache_at = 0.0
        self.last_metrics = {}
        self.last_health_status = "unknown"

    @staticmethod
    def _first_text(card, selectors, timeout=1200):
        for selector in selectors:
            try:
                locator = card.locator(selector)
                if locator.count():
                    text = locator.first.inner_text(timeout=timeout).strip()
                    if text:
                        return text
            except Exception:
                continue
        return ""

    @staticmethod
    def _first_attr(card, selectors, attribute):
        for selector in selectors:
            try:
                locator = card.locator(selector)
                if locator.count():
                    value = (locator.first.get_attribute(attribute) or "").strip()
                    if value:
                        return value
            except Exception:
                continue
        return ""

    def _parse_card(self, card):
        attrs = []
        for name in ("data-csa-c-item-id", "data-promotion-id", "data-coupon-id"):
            try:
                attrs.append((card.get_attribute(name) or "").strip())
            except Exception:
                pass
        href = self._first_attr(card, (
            "a[data-testid='product-card-link']", "a[href*='/dp/']",
            "a[href*='/gp/product/']",
        ), "href")
        try:
            asin = (card.get_attribute("data-asin") or "").strip().upper()
        except Exception:
            asin = ""
        if not asin:
            match = _ASIN_URL_RE.search(href)
            asin = match.group(1).upper() if match else ""
        if not re.fullmatch(r"[A-Z0-9]{10}", asin):
            return None, "missing_asin"

        promo_id = ""
        for raw in attrs:
            match = _PROMO_RE.search(raw) or _PROMO_FALLBACK_RE.search(raw)
            if match:
                promo_id = match.group(1)
                break
        if not promo_id:
            return None, "missing_promo"

        try:
            text = card.inner_text(timeout=3000).replace("\xa0", " ")
        except Exception:
            return None, "card_unreadable"
        final_match = _FINAL_RE.search(text)
        if not final_match:
            final_match = re.search(
                r"(?:preço|valor)\s+(?:final|com cupom)[^R]{0,20}R\$\s*([\d.\s]+(?:,\d{1,2})?)",
                text, re.I,
            )
        final = _money(f"R$ {final_match.group(1)}") if final_match else 0
        current = _money(self._first_text(card, (
            "[data-testid='price-section'] .a-price .a-offscreen",
            "[data-testid*='price'] .a-price .a-offscreen",
            ".a-price:not(.a-text-price) .a-offscreen",
        )))
        reference = _money(self._first_text(card, (
            "[data-testid='price-section'] .a-price.a-text-price .a-offscreen",
            ".a-text-price .a-offscreen",
        ))) or current
        if current <= 0 or final <= 0:
            return None, "invalid_price"
        if final >= current:
            return None, "final_not_lower"

        title = self._first_text(card, (
            ".a-truncate-full", "[data-testid*='title']", "h2", "h3",
        ))
        image_url = self._first_attr(card, ("img",), "src")
        if not title:
            title = self._first_attr(card, ("img",), "alt")
        if not title:
            return None, "missing_title"
        if not image_url:
            return None, "missing_image"
        discount = round((current - final) * 100 / current, 2)
        if discount <= 0 or discount >= 90:
            return None, "implausible_discount"
        return {
            "asin": asin, "promo_id": promo_id, "title": title[:255],
            "url": _canonical_product_url(href, asin),
            "image_url": image_url.split("?", 1)[0][:1000],
            "current": current,
            "reference": reference if reference >= current else current,
            "final": final, "discount": discount,
        }, ""

    def _snapshot(self):
        # run_source chama discover_offers e discover_coupons em sequência. Reusar o
        # mesmo DOM evita abrir a Amazon duas vezes e reduz a chance de bloqueio.
        if self._cache_at and time.monotonic() - self._cache_at < 120:
            return list(self._cache)

        rows = []
        rejected = defaultdict(int)
        metrics = {
            "cards_seen": 0, "accepted": 0, "duplicates": 0,
            "pages_processed": 0, "blocked": 0,
            # ``complete`` é a autorização explícita para materializar
            # ``not_found``. Bater num teto operacional produz catálogo parcial e
            # nunca pode transformar os itens que ficaram depois do teto em
            # descartes.
            "complete": False, "stop_reason": "not_started",
            "duration_by_stage_ms": {
                "navigation": 0, "parsing": 0, "pagination_wait": 0,
            },
        }
        started = time.monotonic()
        max_pages = max(1, settings.AMAZON_COUPON_MAX_PAGES)
        max_items = max(1, settings.AMAZON_COUPON_MAX_ITEMS)
        max_seconds = max(10, settings.AMAZON_COUPON_MAX_SECONDS)
        seen_cards, accepted_keys, selected_schemas = set(), set(), set()
        stable_rounds = 0
        with iniciar_browser(headless=True) as (page, _):
            navigation_started = time.monotonic()
            page.goto(COUPONS_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1800)
            metrics["duration_by_stage_ms"]["navigation"] = round(
                (time.monotonic() - navigation_started) * 1000
            )
            for page_number in range(max_pages):
                parsing_started = time.monotonic()
                body = page.locator("body").inner_text(timeout=10000)
                folded = body.casefold()
                if any(marker in folded for marker in (
                    "digite os caracteres", "não é um robô", "not a robot",
                )):
                    metrics["blocked"] = 1
                    metrics["stop_reason"] = "captcha_or_block"
                    metrics["duration_by_stage_ms"]["parsing"] += round(
                        (time.monotonic() - parsing_started) * 1000
                    )
                    metrics["duration_ms"] = round((time.monotonic() - started) * 1000)
                    self.last_metrics = {
                        **metrics, "rejected": sum(rejected.values()),
                        "rejected_by_reason": dict(sorted(rejected.items())),
                    }
                    self.last_health_status = "blocked"
                    capture_public_diagnostic(page, self.slug, "captcha_or_block")
                    raise RuntimeError("captcha")
                cards = None
                for selector in (
                    "[data-testid='product-card']", "[data-asin]",
                    "li:has(a[href*='/dp/'])", "div:has(> a[href*='/dp/'])",
                ):
                    candidate = page.locator(selector)
                    if candidate.count():
                        cards = candidate
                        selected_schemas.add(selector)
                        break
                metrics["pages_processed"] += 1
                new_this_round = 0
                if cards is not None:
                    for index in range(cards.count()):
                        if time.monotonic() - started >= max_seconds or len(rows) >= max_items:
                            break
                        card = cards.nth(index)
                        try:
                            raw = "|".join(filter(None, (
                                card.get_attribute("data-asin") or "",
                                card.get_attribute("data-csa-c-item-id") or "",
                                card.inner_text(timeout=1200)[:300],
                            )))
                            fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                        except Exception:
                            fingerprint = f"round:{page_number}:card:{index}"
                        if fingerprint in seen_cards:
                            continue
                        seen_cards.add(fingerprint)
                        metrics["cards_seen"] += 1
                        try:
                            row, reason = self._parse_card(card)
                        except Exception as exc:
                            logger.warning(
                                "Card Amazon rejeitado por parse (%s)", type(exc).__name__,
                            )
                            row, reason = None, "parse_error"
                        if row is None:
                            rejected[reason or "invalid"] += 1
                            continue
                        key = (row["promo_id"], row["asin"])
                        if key in accepted_keys:
                            metrics["duplicates"] += 1
                            continue
                        accepted_keys.add(key)
                        rows.append(row)
                        new_this_round += 1
                elif page_number == 0:
                    capture_public_diagnostic(page, self.slug, "cards_not_found")
                stable_rounds = stable_rounds + 1 if new_this_round == 0 else 0
                metrics["duration_by_stage_ms"]["parsing"] += round(
                    (time.monotonic() - parsing_started) * 1000
                )
                if stable_rounds >= 3:
                    metrics["complete"] = True
                    metrics["stop_reason"] = "no_new_items"
                    break
                if len(rows) >= max_items:
                    metrics["stop_reason"] = "max_items"
                    break
                if time.monotonic() - started >= max_seconds:
                    metrics["stop_reason"] = "max_seconds"
                    break
                if page_number + 1 >= max_pages:
                    metrics["stop_reason"] = "max_pages"
                    break
                clicked = False
                pagination_started = time.monotonic()
                for selector in (
                    "button:has-text('Ver mais')", "button:has-text('Carregar mais')",
                    "a:has-text('Próxima')", "button[aria-label*='próxima' i]",
                ):
                    try:
                        button = page.locator(selector)
                        if button.count() and button.first.is_visible():
                            button.first.click(timeout=2000)
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1200)
                metrics["duration_by_stage_ms"]["pagination_wait"] += round(
                    (time.monotonic() - pagination_started) * 1000
                )

        metrics["accepted"] = len(rows)
        metrics["rejected"] = sum(rejected.values())
        metrics["rejected_by_reason"] = dict(sorted(rejected.items()))
        metrics["schema_fingerprint"] = (
            hashlib.sha256(
                "|".join(sorted(selected_schemas)).encode("utf-8")
            ).hexdigest()
            if selected_schemas else ""
        )
        metrics["duration_ms"] = round((time.monotonic() - started) * 1000)
        self.last_metrics = metrics
        self.last_health_status = (
            "ok" if rows and metrics["complete"]
            else "partial" if rows
            else "degraded"
        )

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
                # 'current' é a vitrine, antes de ativar o cupom; 'final' é o que
                # o cliente paga. calcular_precos() depende do par (current, final)
                # para validar o desconto, então os dois viajam juntos.
                effective_price=row["final"],
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
            return {"ok": False, "erro": "Falha temporária na fonte pública.",
                    "cause": type(exc).__name__}
