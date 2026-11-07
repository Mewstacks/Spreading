"""Outbox idempotente: o banco é a fonte de verdade do envio."""
import hashlib
import random
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.compose.notice import MAX_CODES, build_coupon_notice
from apps.coupons.feed import BRT
from apps.coupons.models import Coupon
from apps.orgs.models import ContainerLink

from .models import CouponDelivery, OutboxItem
from .transports import CONFIRMED, UNCERTAIN

MAX_ATTEMPTS = 5


def _today(now):
    return now.astimezone(BRT).date()


def _blocked(destination, now) -> bool:
    if not destination.active:
        return True
    if destination.paused_until and destination.paused_until > now:
        return True
    if destination.next_allowed_at and destination.next_allowed_at > now:
        return True
    return False


def _sent_today(org, destination, now) -> int:
    start = now.astimezone(BRT).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        OutboxItem.objects.for_org(org)
        .filter(destination=destination, created_at__gte=start)
        .exclude(state=OutboxItem.FAILED)
        .count()
    )


def publishable_coupons(org, destination, now):
    """Cupons ativos, vistos no feed há ≤ FEED_MAX_AGE e ainda não entregues ao destino."""
    fresh_since = now - timedelta(seconds=settings.FEED_MAX_AGE_SECONDS)
    delivered = CouponDelivery.objects.for_org(org).filter(destination=destination)
    return (
        Coupon.objects.filter(
            store="mercadolivre", valid_until__gte=_today(now), last_seen__gte=fresh_since
        )
        .exclude(id__in=delivered.values("coupon_id"))
        .exclude(container_url="")
        .order_by("valid_until", "-discount_pct", "code")
    )


def enqueue_coupon_notice(org, destination, now=None):
    """Enfileira um aviso com até 3 cupons do mesmo container. Devolve o item ou None."""
    now = now or timezone.now()
    destination.refresh_from_db()  # intervalo/pausa mudam durante a entrega
    if _blocked(destination, now):
        return None
    if OutboxItem.objects.for_org(org).filter(
        destination=destination, state__in=[OutboxItem.QUEUED, OutboxItem.SENDING]
    ).exists():
        return None
    if _sent_today(org, destination, now) >= destination.daily_cap:
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
    key = hashlib.sha256(key_src.encode()).hexdigest()
    try:
        with transaction.atomic():
            item = OutboxItem.unscoped.create(
                org=org, destination=destination, body=body, idempotency_key=key,
                next_attempt_at=now,
            )
            for coupon in coupons:
                CouponDelivery.unscoped.create(
                    org=org, destination=destination, coupon=coupon, outbox_item=item
                )
    except IntegrityError:
        return None
    return item


def deliver_due(transport, now=None, limit=20) -> dict:
    """Entrega itens vencidos. Cada destino respeita intervalo mínimo e pausa."""
    now = now or timezone.now()
    stats = {"confirmado": 0, "incerto": 0, "falhou": 0, "retry": 0, "bloqueado": 0}
    skipped = set()
    for _ in range(limit):
        claimed = _claim_skipping(now, skipped)
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


def _claim_skipping(now, skipped):
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
            # Nunca reenvia sozinho; os códigos continuam marcados como entregues.
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
            CouponDelivery.unscoped.filter(outbox_item=item).delete()  # pode tentar de novo depois
        item.save()
