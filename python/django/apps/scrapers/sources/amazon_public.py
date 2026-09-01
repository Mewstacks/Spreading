import re
import time
from contextlib import contextmanager
from urllib.parse import quote_plus
from django.conf import settings
from django.utils import timezone

from apps.scrapers.auxiliar import iniciar_browser
from apps.scrapers.carga import BrowserResourceUnavailable, browser_resource
from apps.scrapers.coupon_rules import normalizar_regras_cupom
from .base import IngestedItem, SourceAdapter, normalizar_dinheiro

ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})", re.I)
_FINAL_COUPON_RE = re.compile(
    r"Você paga\s+R\$\s*([\d.\s]+(?:,\d{1,2})?)\s+com o cupom", re.I,
)


class AmazonPublicPageError(RuntimeError):
    def __init__(self, reason):
        self.reason = str(reason or "invalid_page")[:64]
        super().__init__(self.reason)


def _page_failure(status, title="", body=""):
    """Classifica respostas que não representam um inventário Amazon válido."""
    try:
        status = int(status or 0)
    except (TypeError, ValueError):
        status = 0
    if status == 429:
        return "http_429_rate_limited"
    if status >= 500:
        return f"http_{status}_upstream_unavailable"
    if status >= 400:
        return f"http_{status}_error"
    folded = f"{title}\n{body}".casefold()
    if "algo deu errado" in folded:
        return "amazon_error_page"
    if any(marker in folded for marker in (
        "digite os caracteres", "não é um robô", "not a robot",
    )):
        return "captcha_or_block"
    return ""


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


def _preco_final_de_cupom(text, current):
    """Extrai somente o selo inequívoco da Amazon, nunca a palavra no título.

    Buscar por ``cupom`` sozinho encontra livros, impressoras fiscais e brindes que
    contêm a palavra no nome. A frase abaixo foi observada no card oficial e afirma
    simultaneamente a ativação e o preço final.
    """
    match = _FINAL_COUPON_RE.search(str(text or "").replace("\xa0", " "))
    final = _money(match.group(1)) if match else 0.0
    if current <= 0 or final <= 0 or final >= current:
        return 0.0
    discount = (current - final) * 100 / current
    if discount <= 0 or discount >= 90:
        return 0.0
    return final


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
    inventario_completo = False

    def __init__(self):
        self.last_metrics = {}
        self.last_health_status = "healthy"
        self._coupon_cache = []

    def discover_offers(self, terms=None, **kwargs):
        terms = terms or getattr(settings, "AMAZON_FEED_KEYWORDS", []) or ["ofertas"]
        selected, total_terms, offset = _termos_do_ciclo(terms)
        started = time.monotonic()
        processed = 0
        failures = []
        capacity_yielded = False
        rows = 0
        coupon_rows = 0
        pages_processed = 0
        seen = set()
        self._coupon_cache = []
        pages_per_term = max(
            1, int(getattr(settings, "AMAZON_PUBLIC_PAGES_PER_TERM", 3)),
        )
        try:
            with _browser_slot("amazon_public_offers"), \
                    iniciar_browser(headless=True) as (page, _):
                for term in selected:
                    term_complete = True
                    for page_number in range(1, pages_per_term + 1):
                        try:
                            response = page.goto(
                                f"https://www.amazon.com.br/s?k={quote_plus(term)}"
                                f"&page={page_number}",
                                wait_until="domcontentloaded", timeout=25000,
                            )
                            body = page.locator("body").inner_text(timeout=5000)
                            failure = _page_failure(
                                response.status if response else 0,
                                page.title(), body,
                            )
                            if failure:
                                raise AmazonPublicPageError(failure)
                            cards = page.locator("[data-component-type='s-search-result']")
                            pages_processed += 1
                            for index in range(cards.count()):
                                card = cards.nth(index)
                                try:
                                    links = card.locator("a[href*='/dp/']")
                                    url = (
                                        links.first.get_attribute("href", timeout=2000)
                                        if links.count() else ""
                                    )
                                    asin = str(card.get_attribute("data-asin") or "").upper()
                                    match = ASIN_RE.search(url or "")
                                    asin = asin or (match.group(1).upper() if match else "")
                                    if not re.fullmatch(r"[A-Z0-9]{10}", asin) or asin in seen:
                                        continue
                                    title = card.locator("h2").first.inner_text(
                                        timeout=2000,
                                    ).strip()
                                    current = _money(
                                        card.locator(".a-price .a-offscreen").first.inner_text(
                                            timeout=2000,
                                        )
                                    )
                                    previous = 0
                                    old = card.locator(".a-price.a-text-price .a-offscreen")
                                    if old.count():
                                        previous = _money(old.first.inner_text(timeout=1000))
                                    card_text = card.inner_text(timeout=2500)
                                    coupon_final = _preco_final_de_cupom(card_text, current)
                                    if not coupon_final and not _precos_publicaveis(
                                        current, previous,
                                    ):
                                        continue
                                    image = card.locator("img.s-image")
                                    image_url = (
                                        image.first.get_attribute("src", timeout=1000)
                                        if image.count() else ""
                                    ) or ""
                                    seen.add(asin)
                                    rows += 1
                                    observed = timezone.now()
                                    canonical = f"https://www.amazon.com.br/dp/{asin}"
                                    evidence = {
                                        "transport": "amazon-official-search",
                                        "term": term, "page": page_number,
                                    }
                                    effective = 0
                                    reference = previous
                                    if coupon_final:
                                        discount = round(
                                            (current - coupon_final) * 100 / current, 2,
                                        )
                                        promotion_id = f"search:{asin}"
                                        evidence.update({
                                            "association": "amazon-official-search-coupon",
                                            "coupon_final_price": coupon_final,
                                            "promotion": {
                                                "present": True,
                                                "coupon_confirmed": True,
                                                "id": promotion_id,
                                                "label": f"{discount:g}% off",
                                            },
                                        })
                                        effective = coupon_final
                                        reference = max(previous, current)
                                        self._coupon_cache.append(IngestedItem(
                                            external_id=f"amazon-search-coupon:{asin}",
                                            marketplace="amazon", source=self.slug,
                                            kind="coupon", canonical_url=canonical,
                                            title=f"Cupom Amazon — {discount:g}% OFF em {title}"[:255],
                                            coupon_rules=normalizar_regras_cupom({
                                                "tipo_desconto": "porcentagem",
                                                "valor_desconto": discount,
                                                "modo_resgate": "ativacao",
                                                "escopo": "produto selecionado",
                                            }, external_id=f"amazon-search-coupon:{asin}"),
                                            content_type="promotion", observed_at=observed,
                                            evidence={
                                                "transport": "amazon-official-search",
                                                "association": "amazon-official-search-coupon",
                                                "promotion_id": promotion_id,
                                                "asins": [asin],
                                                "coupon_final_price": coupon_final,
                                                "term": term, "page": page_number,
                                            },
                                        ))
                                        coupon_rows += 1
                                    yield IngestedItem(
                                        external_id=asin, marketplace="amazon",
                                        source=self.slug, kind="offer",
                                        canonical_url=canonical, title=title[:255],
                                        current_price=current,
                                        effective_price=effective,
                                        reference_price=reference,
                                        image_url=image_url[:1000],
                                        observed_at=observed, evidence=evidence,
                                    )
                                except Exception:
                                    continue
                        except Exception as exc:
                            failures.append({
                                "term": term, "page": page_number,
                                "error": getattr(exc, "reason", type(exc).__name__),
                            })
                            term_complete = False
                            break
                        from apps.scrapers.resource_control import interesse_pendente
                        if interesse_pendente(
                            "django_chromium", exceto=f"source_{self.slug}",
                        ):
                            capacity_yielded = True
                            term_complete = False
                            break
                    if term_complete:
                        processed += 1
                    if capacity_yielded:
                        break
        finally:
            # É uma fatia rotativa e limitada por páginas, não prova exaustão do
            # catálogo inteiro da Amazon mesmo quando a fatia termina saudável.
            slice_complete = (
                processed == len(selected) and not failures and not capacity_yielded
            )
            self.last_metrics = {
                "rows": rows,
                "coupon_rows": coupon_rows,
                "pages_processed": pages_processed,
                "pages_per_term": pages_per_term,
                "terms_total": total_terms,
                "terms_selected": len(selected),
                "terms_processed": processed,
                "terms_offset": offset,
                "failures": failures,
                "capacity_yielded": capacity_yielded,
                "slice_complete": slice_complete,
                "complete": False,
                "stop_reason": "max_pages" if slice_complete else (
                    "capacity_yielded" if capacity_yielded else "partial_failure"
                ),
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
            self.last_health_status = (
                "healthy_empty" if slice_complete and not rows
                else "healthy" if slice_complete
                else "degraded"
            )

    def discover_coupons(self, **kwargs):
        yield from self._coupon_cache

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
