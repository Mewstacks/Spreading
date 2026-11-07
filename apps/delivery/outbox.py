"""Outbox idempotente: o banco é a fonte de verdade do envio."""
import hashlib
import random
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.compose.notice import MAX_CODES, build_coupon_notice
from apps.compose.offer import build_offer_message
from apps.coupons.feed import BRT
from apps.coupons.models import Coupon
from apps.offers import catalog, ml_api
from apps.offers.models import Rule
from apps.offers.selection import in_window, pick_offer
from apps.orgs.models import ContainerLink

from .models import CouponDelivery, OfferDelivery, OutboxItem
from .transports import CONFIRMED, UNCERTAIN

MAX_ATTEMPTS = 5


def _today_start(now):
    return now.astimezone(BRT).replace(hour=0, minute=0, second=0, microsecond=0)


def _blocked(destination, now) -> bool:
    if not destination.active:
        return True
    if destination.paused_until and destination.paused_until > now:
        return True
    if destination.next_allowed_at and destination.next_allowed_at > now:
        return True
    return False


def _sent_today(org, destination, now) -> int:
    return (
        OutboxItem.objects.for_org(org)
        .filter(destination=destination, created_at__gte=_today_start(now))
        .exclude(state=OutboxItem.FAILED)
        .count()
    )


def _destination_ready(org, destination, now) -> bool:
    destination.refresh_from_db()  # intervalo/pausa mudam durante a entrega
    if _blocked(destination, now):
        return False
    pending = OutboxItem.objects.for_org(org).filter(
        destination=destination, state__in=[OutboxItem.QUEUED, OutboxItem.SENDING]
    )
    if pending.exists():
        return False
    return _sent_today(org, destination, now) < destination.daily_cap


def _rule_ready(org, rule, now) -> bool:
    if not rule.active or not in_window(rule, now):
        return False
    items = OutboxItem.objects.for_org(org).filter(rule=rule).exclude(state=OutboxItem.FAILED)
    last = items.order_by("-created_at").first()
    if last and last.created_at + timedelta(minutes=rule.interval_minutes) > now:
        return False
    return items.filter(created_at__gte=_today_start(now)).count() < rule.effective_cap


def publishable_coupons(org, destination, now):
    """Cupons ativos, vistos no feed há ≤ FEED_MAX_AGE e ainda não entregues ao destino."""
    fresh_since = now - timedelta(seconds=settings.FEED_MAX_AGE_SECONDS)
    delivered = CouponDelivery.objects.for_org(org).filter(destination=destination)
    return (
        Coupon.objects.filter(
            store="mercadolivre",
            valid_until__gte=now.astimezone(BRT).date(),
            last_seen__gte=fresh_since,
        )
        .exclude(id__in=delivered.values("coupon_id"))
        .exclude(container_url="")
        .order_by("valid_until", "-discount_pct", "code")
    )


def enqueue_coupon_notice(org, destination, now=None, rule=None):
    """Enfileira um aviso com até 3 cupons do mesmo container. Devolve o item ou None."""
    now = now or timezone.now()
    if not _destination_ready(org, destination, now):
        return None
    if rule is not None and not _rule_ready(org, rule, now):
        return None

    links = {
        link.container_url: link.affiliate_url
        for link in ContainerLink.objects.for_org(org).exclude(affiliate_url="")
    }
    groups = defaultdict(list)
    for coupon in publishable_coupons(org, destination, now):
        if coupon.container_url in links:  # falha fechada: sem link do dono, não envia
            groups[coupon.container_url].append(coupon)
    if not groups:
        return None

    container, coupons = next(iter(groups.items()))
    coupons = coupons[:MAX_CODES]
    body = build_coupon_notice(coupons, links[container])
    key_src = "|".join(
        [str(org.id), str(destination.id)] + sorted(f"{c.code}:{c.valid_until}" for c in coupons)
    )
    try:
        with transaction.atomic():
            item = OutboxItem.unscoped.create(
                org=org, destination=destination, rule=rule, body=body, created_at=now,
                idempotency_key=hashlib.sha256(key_src.encode()).hexdigest(),
                next_attempt_at=now,
            )
            for coupon in coupons:
                CouponDelivery.unscoped.create(
                    org=org, destination=destination, coupon=coupon, outbox_item=item
                )
    except IntegrityError:
        return None
    return item


def enqueue_offer(org, rule, now=None, live_check=True):
    """Enfileira a melhor oferta publicável da regra. Devolve o item ou None."""
    now = now or timezone.now()
    destination = rule.destination
    if not _destination_ready(org, destination, now) or not _rule_ready(org, rule, now):
        return None
    picked = pick_offer(org, destination, rule, now)
    if not picked:
        return None
    offer, link = picked
    if live_check and offer.source == catalog.SOURCE:
        live = ml_api.product_price(offer.product_key)
        if not live or Decimal(str(live["price"])) != offer.price_to:
            return None  # preço mudou ou sumiu desde a coleta: não anuncia número velho
    key_src = f"{org.id}|{destination.id}|{offer.product_key}|{offer.valid_on}"
    try:
        with transaction.atomic():
            item = OutboxItem.unscoped.create(
                org=org, destination=destination, rule=rule, kind="offer", created_at=now,
                body=build_offer_message(offer, link), next_attempt_at=now,
                idempotency_key=hashlib.sha256(key_src.encode()).hexdigest(),
            )
            OfferDelivery.unscoped.create(
                org=org, destination=destination, product_key=offer.product_key,
                outbox_item=item, created_at=now,
            )
    except IntegrityError:
        return None
    return item


def enqueue_for_rule(rule, now=None, live_check=True):
    org = rule.org
    if rule.type == Rule.COUPON_NOTICE:
        return enqueue_coupon_notice(org, rule.destination, now, rule=rule)
    return enqueue_offer(org, rule, now, live_check=live_check)


def deliver_due(transport, now=None, limit=20) -> dict:
    """Entrega itens vencidos. Cada destino respeita intervalo mínimo e pausa."""
    now = now or timezone.now()
    stats = {"confirmado": 0, "incerto": 0, "falhou": 0, "retry": 0, "bloqueado": 0}
    skipped = set()
    for _ in range(limit):
        claimed = _claim(now, skipped)
        if claimed is None:
            break
        status, item = claimed
        if status == "blocked":
            skipped.add(item.id)
            stats["bloqueado"] += 1
            continue
        result = transport.send_text(item.destination.external_id, item.body)
        _finish(item, result, now)
        if result.outcome == CONFIRMED:
            stats["confirmado"] += 1
        elif result.outcome == UNCERTAIN:
            stats["incerto"] += 1
        elif item.state == OutboxItem.QUEUED:
            stats["retry"] += 1
        else:
            stats["falhou"] += 1
    return stats


def _claim(now, skipped):
    with transaction.atomic():
        item = (
            OutboxItem.unscoped.select_for_update(skip_locked=True)
            .select_related("destination")
            .filter(state=OutboxItem.QUEUED, next_attempt_at__lte=now)
            .exclude(id__in=skipped)
            .order_by("next_attempt_at", "id")
            .first()
        )
        if item is None:
            return None
        if _blocked(item.destination, now):
            return "blocked", item
        item.state = OutboxItem.SENDING
        item.attempts += 1
        item.save(update_fields=["state", "attempts"])
        return "claimed", item


def _finish(item, result, now):
    destination = item.destination
    with transaction.atomic():
        if result.outcome == CONFIRMED:
            item.state = OutboxItem.CONFIRMED
            item.external_message_id = result.message_id
            item.sent_at = now
            destination.next_allowed_at = now + timedelta(seconds=destination.min_interval_seconds)
            destination.save(update_fields=["next_allowed_at"])
        elif result.outcome == UNCERTAIN:
            # Nunca reenvia sozinho; os itens continuam marcados como entregues.
            item.state = OutboxItem.UNCERTAIN
            item.last_error = result.error[:300]
        elif result.retry and item.attempts < MAX_ATTEMPTS:
            item.state = OutboxItem.QUEUED
            delay = 30 * (2 ** (item.attempts - 1))
            item.next_attempt_at = now + timedelta(seconds=delay * random.uniform(0.8, 1.2))
            item.last_error = result.error[:300]
        else:
            item.state = OutboxItem.FAILED
            item.last_error = result.error[:300]
            # volta a ser elegível
            CouponDelivery.unscoped.filter(outbox_item=item).delete()
            OfferDelivery.unscoped.filter(outbox_item=item).delete()
        item.save()
