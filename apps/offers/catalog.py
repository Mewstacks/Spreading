"""Coletor de catálogo do ML pela API oficial, guiado pelos nichos e termos das regras ativas."""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from apps.coupons.feed import BRT

from . import ml_api, niches
from .models import Offer, Rule, TermRun

SOURCE = "ml-api-catalogo"
REFRESH_EVERY = timedelta(hours=1)  # o ciclo roda de 5 em 5 min; a API não precisa disso


def split_terms(text: str) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def rule_terms(rule) -> list[str]:
    """Termos de busca de uma regra: sub-nichos escolhidos + termos extras."""
    terms = split_terms(rule.terms)
    for key in rule.niches or []:
        if key.startswith("s:"):
            terms.extend(t for t in niches._sub_terms(key))
    return terms


def search_terms(rule, general: bool = False) -> list[str]:
    """Termos de busca para uma loja sem navegador (Shopee): sub-nichos, termos extras e, para
    quem marcou só a macro, as palavras principais dela. `general` devolve [""] (listagem geral)
    quando a regra não restringe nada."""
    terms = rule_terms(rule)
    for key in rule.niches or []:
        if key.startswith("m:"):
            for macro, keywords in niches.MACROS:
                if macro == key[2:]:
                    terms.extend(k.strip() for k in keywords[:4])
    terms = list(dict.fromkeys(t for t in terms if t))
    return terms or ([""] if general and not rule.niches else [])


def collect_catalog(now=None, per_term=15, http=None) -> int:
    now = now or timezone.now()
    today = now.astimezone(BRT).date()
    terms = set()
    for rule in Rule.unscoped.filter(active=True, type=Rule.OFFERS):
        terms.update(rule_terms(rule))
    last = dict(TermRun.objects.filter(term__in={t[:120] for t in terms})
                .values_list("term", "last_run"))
    count = 0
    for term in sorted(terms):
        if term[:120] in last and now - last[term[:120]] < REFRESH_EVERY:
            continue
        TermRun.objects.update_or_create(term=term[:120], defaults={"last_run": now})
        for product in ml_api.search_products(term, per_term, http=http):
            price = ml_api.product_price(product["id"], http=http)
            if not price or not price["price"]:
                continue
            price_to = Decimal(str(price["price"]))
            price_from = Decimal(str(price["price_from"])) if price["price_from"] else None
            if not price_from or price_from <= price_to or not price["winner_tagged"]:
                continue  # sem preço de lista oficial ou ganhador marcado: não é provada
            pct = ((1 - price_to / price_from) * 100).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            title = str(product.get("name") or product["id"])[:300]
            url = f"https://www.mercadolivre.com.br/p/{product['id']}"
            pictures = product.get("pictures") or []
            first = pictures[0] if pictures and isinstance(pictures[0], dict) else {}
            image = first.get("url") or ""
            Offer.objects.update_or_create(
                store="mercadolivre", link_key=product["id"], valid_on=today,
                defaults={
                    "source": SOURCE, "product_key": product["id"], "source_url": url,
                    "title": title, "url": url, "image_url": image,
                    "macro": niches.classify(title),
                    "price_from": price_from, "price_to": price_to, "discount_pct": pct,
                    "last_seen": now,
                },
            )
            count += 1
    return count
