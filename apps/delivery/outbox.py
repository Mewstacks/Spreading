"""Outbox idempotente: o banco é a fonte de verdade do envio."""
import hashlib
import random
from collections import Counter, defaultdict
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.compose.image import fetch_image_jpeg
from apps.compose.notice import MAX_CODES, build_coupon_notice
from apps.compose.offer import build_offer_message
from apps.coupons.feed import BRT
from apps.coupons.models import Coupon
from apps.offers import live, selection, shopee
from apps.offers.keys import title_identity
from apps.offers.models import AffiliateLink, Rule
from apps.orgs.models import ContainerLink

from .models import CouponDelivery, OfferDelivery, OutboxItem
from .transports import CONFIRMED, FAILED, PERMANENT, UNCERTAIN, Result

MAX_ATTEMPTS = 5
MAX_LIVE_TRIES = 8  # candidatos conferidos por tique (como o v1: tenta até 8)
INTERVAL_JITTER = 0.25  # o intervalo da regra varia ±25% por envio: o ritmo não vira relógio


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


def _session_has_room(destination, now) -> bool:
    """Teto por hora do número de WhatsApp, somando todos os grupos que ele atende."""
    cap = settings.SESSION_HOURLY_CAP
    if not cap:
        return True
    recent = (
        OutboxItem.unscoped.filter(destination__session=destination.session,
                                   created_at__gte=now - timedelta(hours=1))
        .exclude(state=OutboxItem.FAILED).count()
    )
    return recent < cap


def _destination_ready(org, destination, now) -> bool:
    destination.refresh_from_db()  # intervalo/pausa mudam durante a entrega
    if _blocked(destination, now) or not _session_has_room(destination, now):
        return False
    pending = OutboxItem.objects.for_org(org).filter(
        destination=destination, state__in=[OutboxItem.QUEUED, OutboxItem.SENDING]
    )
    if pending.exists():
        return False
    return _sent_today(org, destination, now) < destination.daily_cap


def jittered_interval(rule, last) -> timedelta:
    """Intervalo da regra com variação fixa por envio (mesmo envio, mesma espera: sem sorteio
    a cada ciclo, que faria a regra "tentar a sorte" de 5 em 5 minutos)."""
    digest = hashlib.sha256(f"{rule.id}:{last.id}".encode()).digest()
    spread = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF * 2 - 1  # de -1 a 1
    return timedelta(minutes=rule.interval_minutes * (1 + INTERVAL_JITTER * spread))


def _rule_ready(org, rule, now) -> bool:
    if not rule.active or not selection.in_window(rule, now):
        return False
    items = OutboxItem.objects.for_org(org).filter(rule=rule).exclude(state=OutboxItem.FAILED)
    last = items.order_by("-created_at").first()
    if last and last.created_at + jittered_interval(rule, last) > now:
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
    """Aviso de cupons (regra opcional `aviso_cupons`): até 3 códigos do mesmo container."""
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


def enqueue_offer(org, rule, now=None, report: Counter | None = None, http=None, force=False):
    """Enfileira a melhor oferta publicável (foto, preço provado ao vivo, link do dono).

    `force` (botão "Enviar agora") ignora janela e intervalo da regra; teto e destino valem.
    """
    now = now or timezone.now()
    report = report if report is not None else Counter()
    destination = rule.destination
    if not _destination_ready(org, destination, now) or not (
        force or _rule_ready(org, rule, now)
    ):
        report["regra_ou_grupo_fora_da_janela"] += 1
        return None
    tries = 0
    for offer, link in selection.candidates(org, destination, rule, now, report):
        if tries >= MAX_LIVE_TRIES:
            break
        tries += 1
        proof, reason = live.validate(offer, rule.min_discount_pct, http=http)
        if proof is None:
            report[reason] += 1
            continue
        if fetch_image_jpeg(offer.image_url) is None:
            report["sem_foto"] += 1  # sem foto, sem post
            continue
        link = _attributed(offer, link, destination)
        item = _create_offer_item(org, rule, destination, offer, link, proof, now)
        if item is None:
            report["ja_enfileirada"] += 1
            continue
        return item
    return None


def _attributed(offer, link, destination, http=None) -> str:
    """Shopee: link curto com os sub-ids do Spreading e do grupo, para a venda voltar atribuída
    (relatório de conversões, tela Receita). Qualquer falha cai no link comum."""
    if offer.store != "shopee" or not settings.SHOPEE_TRACK:
        return link
    return shopee.tracked_link(offer.url, ["spreading", f"g{destination.id}"], http=http) or link


def _create_offer_item(org, rule, destination, offer, link, proof, now):
    """Item do outbox + registro de entrega; None se a oferta já foi enfileirada hoje."""
    offer.price_to, offer.price_from, offer.discount_pct = (
        proof.price_to, proof.price_from, proof.discount_pct
    )
    # Frete e loja oficial do anúncio de agora (só na mensagem, sem gravar na oferta).
    if proof.free_shipping is not None:
        offer.free_shipping = proof.free_shipping
    if proof.official_store is not None:
        offer.official_store = proof.official_store
    key_src = f"{org.id}|{destination.id}|{offer.link_key}|{offer.valid_on}"
    try:
        with transaction.atomic():
            item = OutboxItem.unscoped.create(
                org=org, destination=destination, rule=rule, kind="offer", created_at=now,
                body=build_offer_message(offer, link), next_attempt_at=now,
                image_url=offer.image_url,
                idempotency_key=hashlib.sha256(key_src.encode()).hexdigest(),
            )
            OfferDelivery.unscoped.create(
                org=org, destination=destination, product_key=offer.product_key,
                identity=title_identity(offer.title), outbox_item=item, created_at=now,
            )
    except IntegrityError:
        return None
    return item


def enqueue_specific(org, rule, offer, now=None, http=None):
    """Envio manual de uma oferta escolhida no painel: as mesmas provas do automático (link do
    dono, frescor, cupom no prazo, preço ao vivo, foto), sem janela, intervalo nem cooldown.
    Devolve (item, '') ou (None, motivo)."""
    now = now or timezone.now()
    destination = rule.destination
    if not _destination_ready(org, destination, now):
        return None, "grupo_indisponivel"
    links = dict(AffiliateLink.objects.for_org(org)
                 .filter(product_key__in={offer.link_key, offer.product_key})
                 .values_list("product_key", "affiliate_url"))
    link = selection.resolve_link(org, offer, links)
    if not link:
        return None, "sem_link_do_dono"
    if offer.coupon_until and offer.coupon_until < now:
        return None, "cupom_vencido"
    stale = selection.fresh_enough(offer, now)
    if stale:
        return None, stale
    proof, reason = live.validate(offer, 1, http=http)
    if proof is None:
        return None, reason
    if fetch_image_jpeg(offer.image_url) is None:
        return None, "sem_foto"
    link = _attributed(offer, link, destination)
    item = _create_offer_item(org, rule, destination, offer, link, proof, now)
    return (item, "") if item else (None, "ja_enfileirada")


def explain_rule(rule, now=None, http=None) -> Counter:
    """"Por que não saiu": motivos que barraram ofertas + `prontas` (não grava nada)."""
    now = now or timezone.now()
    report = Counter()
    ready = sum(1 for _ in selection.candidates(rule.org, rule.destination, rule, now, report))
    if ready:
        report["prontas"] = ready
    return report


def enqueue_for_rule(rule, now=None, http=None, force=False):
    org = rule.org
    if rule.type == Rule.COUPON_NOTICE:
        return enqueue_coupon_notice(org, rule.destination, now, rule=rule)
    return enqueue_offer(org, rule, now, http=http, force=force)


def deliver_due(transport, now=None, limit=20, sessions=None) -> dict:
    """Entrega itens vencidos. Cada destino respeita intervalo mínimo e pausa. `sessions`, se vier,
    limita a entrega aos números de WhatsApp conectados (os demais esperam na fila)."""
    now = now or timezone.now()
    stats = {"confirmado": 0, "incerto": 0, "falhou": 0, "retry": 0, "bloqueado": 0}
    skipped = set()
    for _ in range(limit):
        claimed = _claim(now, skipped, sessions)
        if claimed is None:
            break
        status, item = claimed
        if status == "blocked":
            skipped.add(item.id)
            stats["bloqueado"] += 1
            continue
        result = _send(transport, item)
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


def _send(transport, item):
    """Oferta = foto + legenda (sem foto não publica). Aviso de cupons = texto."""
    if hasattr(transport, "for_session"):  # vários números: cada destino sai pelo seu
        transport = transport.for_session(item.destination.session)
    chat = item.destination.external_id
    if item.kind == "offer":
        jpeg = fetch_image_jpeg(item.image_url) if item.image_url else None
        if jpeg is None or not hasattr(transport, "send_image"):
            return Result(False, FAILED, klass=PERMANENT, error="foto_indisponivel",
                          keep_claim=True)
        return transport.send_image(chat, jpeg, item.body)
    return transport.send_text(chat, item.body)


def _claim(now, skipped, sessions=None):
    with transaction.atomic():
        due = (
            OutboxItem.unscoped.select_for_update(skip_locked=True)
            .select_related("destination")
            .filter(state=OutboxItem.QUEUED, next_attempt_at__lte=now)
            .exclude(id__in=skipped)
        )
        if sessions is not None:
            due = due.filter(destination__session__in=sessions)
        item = due.order_by("next_attempt_at", "id").first()
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
            if not result.keep_claim:  # volta a ser elegível
                CouponDelivery.unscoped.filter(outbox_item=item).delete()
                OfferDelivery.unscoped.filter(outbox_item=item).delete()
        item.save()
