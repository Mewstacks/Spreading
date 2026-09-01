import re
import time
from contextlib import contextmanager
from urllib.parse import quote_plus
from django.conf import settings
from django.utils import timezone

from apps.scrapers.auxiliar import iniciar_browser
from apps.scrapers.carga import BrowserResourceUnavailable, browser_resource
from .base import IngestedItem, SourceAdapter, normalizar_dinheiro

ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})", re.I)


def _termos_do_ciclo(terms, agora=None, limite=None):
    """Seleciona uma fatia rotativa sem perder cobertura do catalogo."""
    unicos = list(dict.fromkeys(str(term).strip() for term in terms if str(term).strip()))
    if not unicos:
        unicos = ["ofertas"]
    limite = max(1, int(
        limite or getattr(settings, "AMAZON_PUBLIC_TERMS_PER_CYCLE", 2)
    ))
    limite = min(limite, len(unicos))
    agora = agora or timezone.now()
    janela = int(agora.timestamp() // (3 * 60 * 60))
    offset = (janela * limite) % len(unicos)
    selecionados = [unicos[(offset + indice) % len(unicos)] for indice in range(limite)]
    return selecionados, len(unicos), offset


def _money(text):
    return normalizar_dinheiro(text)


def _precos_publicaveis(current, previous):
    """Aceita desconto realista e barra preço anterior em escala errada.

    O HTML público já devolveu valores como 63990 para um produto de 63,99.
    A seleção automática considera 90% suspeito; a coleta deve aplicar a mesma
    regra antes de persistir o item.
    """
    return current > 0 and previous > current and previous < current * 10


@contextmanager
def _browser_slot(owner_kind):
    with browser_resource(owner_kind=owner_kind) as acquired:
        if not acquired:
            raise BrowserResourceUnavailable(
                "Capacidade de browser ocupada; a operação Amazon será retomada."
            )
        yield


def verify_product_url(url, nome_esperado=None):
    """Validação JIT pública usada antes de qualquer publicação Amazon."""
    with _browser_slot("amazon_product_verify"), \
            iniciar_browser(headless=True) as (page, _):
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        body = page.locator("body").inner_text(timeout=5000)
        lower = body.lower()
        if "digite os caracteres" in lower:
            return {"ok": False, "motivo": "Amazon solicitou CAPTCHA"}
        if any(term in lower for term in ("não disponível", "indisponível no momento")):
            return {"ok": False, "motivo": "Produto indisponível"}
        title_loc = page.locator("#productTitle")
        title = title_loc.first.inner_text(timeout=2000).strip() if title_loc.count() else ""
        price_loc = page.locator(
            "#corePrice_feature_div .a-price .a-offscreen, "
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen")
        price = _money(price_loc.first.inner_text(timeout=2000)) if price_loc.count() else 0
        if not title or price <= 0:
            return {"ok": False, "motivo": "Preço ou produto não confirmado"}
        return {"ok": True, "titulo": title, "preco": price}


class AmazonPublicSource(SourceAdapter):
    slug = "amazon-public-web"
    marketplace = "amazon"
    name = "Amazon — catálogo público"
    requires_chromium = True

    def __init__(self):
        self.last_metrics = {}
        self.last_health_status = "healthy"

    def discover_offers(self, terms=None, **kwargs):
        terms = terms or getattr(settings, "AMAZON_FEED_KEYWORDS", []) or ["ofertas"]
        selected, total_terms, offset = _termos_do_ciclo(terms)
        started = time.monotonic()
        processed = 0
        failures = []
        capacity_yielded = False
        rows = 0
        seen = set()
        try:
            with _browser_slot("amazon_public_offers"), \
                    iniciar_browser(headless=True) as (page, _):
                for term in selected:
                    try:
                        page.goto(f"https://www.amazon.com.br/s?k={quote_plus(term)}",
                                  wait_until="domcontentloaded", timeout=25000)
                        body = page.locator("body").inner_text(timeout=5000)
                        if "digite os caracteres" in body.lower():
                            raise RuntimeError("captcha")
                        cards = page.locator("[data-component-type='s-search-result']")
                        for index in range(cards.count()):
                            card = cards.nth(index)
                            try:
                                links = card.locator("a[href*='/dp/']")
                                url = links.first.get_attribute("href", timeout=2000) if links.count() else ""
                                match = ASIN_RE.search(url or "")
                                if not match or match.group(1) in seen:
                                    continue
                                asin = match.group(1).upper()
                                title = card.locator("h2").first.inner_text(timeout=2000).strip()
                                current = _money(card.locator(".a-price .a-offscreen").first.inner_text(timeout=2000))
                                previous = 0
                                old = card.locator(".a-price.a-text-price .a-offscreen")
                                if old.count():
                                    previous = _money(old.first.inner_text(timeout=1000))
                                if not _precos_publicaveis(current, previous):
                                    continue
                                seen.add(asin)
                                rows += 1
                                yield IngestedItem(
                                    external_id=asin, marketplace="amazon", source=self.slug,
                                    kind="offer", canonical_url=f"https://www.amazon.com.br/dp/{asin}",
                                    title=title[:255], current_price=current, reference_price=previous,
                                    observed_at=timezone.now(),
                                    evidence={"transport": "public-search", "term": term},
                                )
                            except Exception:
                                continue
                        processed += 1
                    except Exception as exc:
                        failures.append({"term": term, "error": type(exc).__name__})
                    from apps.scrapers.resource_control import interesse_pendente
                    if interesse_pendente(
                        "django_chromium", exceto=f"source_{self.slug}",
                    ):
                        capacity_yielded = True
                        break
        finally:
            complete = processed == len(selected) and not failures and not capacity_yielded
            self.last_metrics = {
                "rows": rows,
                "terms_total": total_terms,
                "terms_selected": len(selected),
                "terms_processed": processed,
                "terms_offset": offset,
                "failures": failures,
                "capacity_yielded": capacity_yielded,
                "complete": complete,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
            self.last_health_status = (
                "healthy_empty" if complete and not rows
                else "healthy" if complete
                else "degraded"
            )

    def discover_coupons(self, **kwargs):
        return []

    def refresh_offer(self, item, **kwargs):
        with _browser_slot("amazon_offer_refresh"), \
                iniciar_browser(headless=True) as (page, _):
            page.goto(item.canonical_url, wait_until="domcontentloaded", timeout=45000)
            body = page.locator("body").inner_text(timeout=5000).lower()
            if "não disponível" in body or "indisponível" in body:
                return None
            price = page.locator("#corePrice_feature_div .a-offscreen, #priceblock_ourprice").first
            current = _money(price.inner_text()) if price.count() else 0
            if not current:
                raise RuntimeError("price missing")
            return IngestedItem(**{**item.__dict__, "current_price": current,
                                  "observed_at": timezone.now()})
