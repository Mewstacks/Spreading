"""Quando uma regra pode publicar e qual oferta ela escolhe (invariantes do v1)."""
import re
import statistics
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings

from apps.coupons.feed import BRT
from apps.delivery.models import OfferDelivery

from . import niches
from .catalog import split_terms
from .links import is_storefront
from .models import AffiliateLink, Offer

ML_CATALOG_ID = re.compile(r"^MLB\d+$")
ML_LIVE_MAX_AGE = timedelta(days=14)  # item de catálogo do ML é conferido ao vivo no envio
OTHER_STORE_MAX_AGE = timedelta(hours=3)  # Amazon/Shopee: sem conferência ao vivo ainda
FEED_SOURCE = "ml-ofertas-diarias"
# Guarda de queda real (herdada do v1): produto com ≥3 outras observações nos últimos 30 dias só
# é anunciado como promoção se o preço da oferta está abaixo do típico; senão o "De" é de fachada.
HISTORY_DAYS = 30
HISTORY_MIN_OTHERS = 3
HISTORY_MARGIN = Decimal("0.97")


def in_window(rule, now) -> bool:
    local = now.astimezone(BRT)
    days = {int(d) for d in rule.weekdays.split(",") if d.strip().isdigit()}
    if days and local.isoweekday() not in days:
        return False
    start, end, hour = rule.window_start, rule.window_end, local.hour
    if start == end:
        return True  # janela de 24 h
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # cruza a meia-noite


def is_live_checkable(offer) -> bool:
    return (
        offer.store == "mercadolivre"
        and bool(ML_CATALOG_ID.match(offer.product_key))
        and "/p/" in offer.url
    )


def term_filter(rule, title: str) -> bool:
    low = niches.fold(title)
    exclude = [niches.fold(t) for t in split_terms(rule.negative_terms)]
    if exclude and any(t in low for t in exclude):
        return False
    include = [niches.fold(t) for t in split_terms(rule.terms)]
    return not include or any(t in low for t in include)


def fresh_enough(offer, now) -> str:
    """'' se a observação é aceitável; senão o motivo."""
    if offer.source == FEED_SOURCE:
        ok = offer.valid_on == now.astimezone(BRT).date() and (
            offer.last_seen >= now - timedelta(seconds=settings.FEED_MAX_AGE_SECONDS)
        )
        return "" if ok else "feed_velho"
    if offer.store == "mercadolivre":
        if not is_live_checkable(offer):
            return "sem_prova_de_preco"  # anúncio solto: a API não mede (403)
        return "" if offer.last_seen >= now - ML_LIVE_MAX_AGE else "observacao_velha"
    return "" if offer.last_seen >= now - OTHER_STORE_MAX_AGE else "sem_prova_de_preco"


def resolve_link(org, offer, links: dict) -> str:
    if offer.store == "amazon":
        return (
            f"https://www.amazon.com.br/dp/{offer.product_key}?tag={org.amazon_tag}"
            if org.amazon_tag and offer.product_key else ""
        )
    url = links.get(offer.link_key, "")
    if not url and offer.store == "mercadolivre" and offer.coupon_kind == "code":
        # Cupom por código vale no carrinho: o link do produto basta. O de ativação precisa de um
        # link que carregue a campanha, então não cai aqui.
        url = links.get(offer.product_key, "")
    return "" if is_storefront(url) else url  # cinto e suspensório: o estoque já recusa na entrada


def score(offer, match_kind: str, now) -> float:
    """100 × (0,40 valor + 0,25 afinidade + 0,20 confiança + 0,10 urgência + 0,05 desempenho)."""
    value = min(float(offer.discount_pct), 60.0) / 60.0
    affinity = {"sub": 1.0, "macro": 0.7}.get(match_kind, 0.5)
    trust = 1.0 if (offer.coupon_kind or offer.source == FEED_SOURCE) else 0.8
    urgent = 0.3
    if offer.source == FEED_SOURCE:
        urgent = 1.0
    elif offer.coupon_until and offer.coupon_until - now <= timedelta(hours=48):
        urgent = 1.0
    # Desempenho: comissão informada pela loja (20% ou mais vale o máximo); sem dado, neutro.
    perf = min(float(offer.commission_pct), 20.0) / 20.0 if offer.commission_pct else 0.5
    total = 0.40 * value + 0.25 * affinity + 0.20 * trust + 0.10 * urgent + 0.05 * perf
    return round(100 * total, 2)


def price_history(offers, now) -> dict:
    """Observações dos últimos 30 dias de cada produto sem cupom, `(id, preço)`; uma consulta só.
    Conta as linhas de todos os dias, para a linha velha de um produto parado também cair."""
    keys = {(o.store, o.link_key) for o in offers if not o.coupon_kind}
    if not keys:
        return {}
    since = now.astimezone(BRT).date() - timedelta(days=HISTORY_DAYS)
    rows = Offer.objects.filter(
        link_key__in={key for _store, key in keys}, valid_on__gte=since,
    ).values_list("id", "store", "link_key", "price_to")
    history = defaultdict(list)
    for row_id, store, key, price in rows:
        if (store, key) in keys:
            history[(store, key)].append((row_id, price))
    return history


def real_drop(offer, history: dict) -> bool:
    """False só quando há observações suficientes e o preço da oferta não está abaixo do típico
    das outras. Par com cupom não entra: o abatimento dele é o cupom, não a oscilação do anúncio."""
    others = [p for row_id, p in history.get((offer.store, offer.link_key)) or []
              if row_id != offer.id]
    if offer.coupon_kind or len(others) < HISTORY_MIN_OTHERS:
        return True
    return offer.price_to <= statistics.median(others) * HISTORY_MARGIN


def candidates(org, destination, rule, now, report: Counter | None = None,
               missing: list | None = None):
    """Ofertas publicáveis em ordem: pares cupom+produto primeiro, depois maior pontuação.

    `missing`, se vier, recebe `(oferta, pontuação)` das ofertas do ML que passaram em tudo e só
    não têm link do dono, na mesma ordem: é a fila do Link Builder.
    """
    report = report if report is not None else Counter()
    links = dict(AffiliateLink.objects.for_org(org).values_list("product_key", "affiliate_url"))
    cooldown_since = now - timedelta(hours=rule.cooldown_hours)
    recent = set(
        OfferDelivery.objects.for_org(org)
        .filter(destination=destination, created_at__gte=cooldown_since)
        .values_list("product_key", flat=True)
    )
    pool = (
        Offer.objects.filter(
            store__in=rule.store_list, discount_pct__gte=rule.min_discount_pct,
            last_seen__gte=now - ML_LIVE_MAX_AGE,
        )
        .exclude(image_url="")
        .exclude(coupon_until__lt=now)
    )
    if rule.coupon_mode == "only":
        pool = pool.exclude(coupon_kind="")
    elif rule.coupon_mode == "off":
        pool = pool.filter(coupon_kind="")
    ranked = []
    for offer in pool.iterator(chunk_size=1000):
        reason = fresh_enough(offer, now)
        if reason:
            report[reason] += 1
            continue
        if offer.product_key in recent:
            report["cooldown"] += 1
            continue
        if rule.price_min is not None and offer.price_to < rule.price_min:
            report["fora_da_faixa_de_preco"] += 1
            continue
        if rule.price_max is not None and offer.price_to > rule.price_max:
            report["fora_da_faixa_de_preco"] += 1
            continue
        macro = offer.macro or niches.classify(offer.title)
        kind = niches.match(list(rule.niches or []), offer.title, macro)
        if not kind or not term_filter(rule, offer.title):
            report["fora_do_nicho"] += 1
            continue
        link = resolve_link(org, offer, links)
        if not link:
            report["sem_link_do_dono"] += 1
            if missing is not None and offer.store == "mercadolivre":
                missing.append((offer, score(offer, kind, now)))
            continue
        ranked.append((offer, link, score(offer, kind, now)))
    history = price_history([row[0] for row in ranked + (missing or [])], now)
    kept = [row for row in ranked if real_drop(row[0], history)]
    if len(kept) != len(ranked):
        report["sem_queda_real"] += len(ranked) - len(kept)
    ranked = kept
    if missing is not None:
        missing[:] = [row for row in missing if real_drop(row[0], history)]
        missing.sort(key=lambda row: (not row[0].coupon_kind, -row[1]))
    ranked.sort(key=lambda row: (not row[0].coupon_kind, -row[2]))
    for offer, link, _score in ranked:
        yield offer, link
