from datetime import date, timedelta
from decimal import Decimal

from apps.coupons.models import Coupon
from apps.delivery.models import CouponDelivery, OutboxItem
from apps.delivery.outbox import deliver_due, enqueue_coupon_notice
from tests.conftest import NOW, FakeTransport, ok, permanent, transient, uncertain

CONTAINER = "https://lista.mercadolivre.com.br/_Container_aff-list-28"


def make_coupon(code, container=CONTAINER, last_seen=NOW, valid_until=date(2026, 11, 15)):
    return Coupon.objects.create(
        code=code, source="t", discount_pct=Decimal("30"), max_discount=Decimal("500"),
        min_purchase=Decimal("1"), valid_until=valid_until, container_url=container,
        last_seen=last_seen,
    )


def test_enqueue_groups_up_to_three_codes_with_owner_link(org, destination, container_link):
    for code in ("A", "B", "C", "D"):
        make_coupon(code)
    item = enqueue_coupon_notice(org, destination, NOW)
    assert item and item.body.count("OFF") == 3 and "https://meli.la/abc123" in item.body
    assert CouponDelivery.objects.for_org(org).count() == 3


def test_no_owner_link_means_no_send(org, destination):
    make_coupon("A")
    assert enqueue_coupon_notice(org, destination, NOW) is None
    assert OutboxItem.objects.for_org(org).count() == 0


def test_stale_feed_is_not_publishable(org, destination, container_link):
    make_coupon("A", last_seen=NOW - timedelta(hours=2))
    assert enqueue_coupon_notice(org, destination, NOW) is None


def test_expired_coupon_is_not_publishable(org, destination, container_link):
    make_coupon("A", valid_until=date(2026, 11, 6))
    assert enqueue_coupon_notice(org, destination, NOW) is None


def test_one_pending_per_destination_and_no_repeat(org, destination, container_link):
    make_coupon("A")
    first = enqueue_coupon_notice(org, destination, NOW)
    assert first
    make_coupon("B")
    assert enqueue_coupon_notice(org, destination, NOW) is None  # já há pendente
    transport = FakeTransport()
    deliver_due(transport, NOW)
    make_coupon("B2")
    later = NOW + timedelta(minutes=5)
    second = enqueue_coupon_notice(org, destination, later)
    assert second and "*A*" not in second.body  # A já foi entregue


def test_deliver_confirms_and_sets_interval(org, destination, container_link):
    make_coupon("A")
    item = enqueue_coupon_notice(org, destination, NOW)
    transport = FakeTransport(ok("wamid.1"))
    stats = deliver_due(transport, NOW)
    item.refresh_from_db()
    destination.refresh_from_db()
    assert stats["confirmado"] == 1 and item.state == OutboxItem.CONFIRMED
    assert item.external_message_id == "wamid.1"
    assert transport.sent[0][0] == "120363@g.us"
    assert destination.next_allowed_at == NOW + timedelta(seconds=60)


def test_min_interval_blocks_next_notice(org, destination, container_link):
    make_coupon("A")
    enqueue_coupon_notice(org, destination, NOW)
    deliver_due(FakeTransport(), NOW)
    make_coupon("B")
    assert enqueue_coupon_notice(org, destination, NOW + timedelta(seconds=30)) is None
    assert enqueue_coupon_notice(org, destination, NOW + timedelta(seconds=61))


def test_daily_cap(org, destination, container_link):
    destination.daily_cap = 1
    destination.save()
    make_coupon("A")
    enqueue_coupon_notice(org, destination, NOW)
    deliver_due(FakeTransport(), NOW)
    make_coupon("B")
    assert enqueue_coupon_notice(org, destination, NOW + timedelta(minutes=10)) is None


def test_paused_destination_does_not_enqueue(org, destination, container_link):
    destination.paused_until = NOW + timedelta(hours=1)
    destination.save()
    make_coupon("A")
    assert enqueue_coupon_notice(org, destination, NOW) is None


def test_transient_failure_retries_with_backoff_then_gives_up(org, destination, container_link):
    make_coupon("A")
    item = enqueue_coupon_notice(org, destination, NOW)
    now = NOW
    for attempt in range(1, 5):
        stats = deliver_due(FakeTransport(transient()), now)
        item.refresh_from_db()
        assert stats["retry"] == 1 and item.state == OutboxItem.QUEUED and item.attempts == attempt
        assert item.next_attempt_at > now
        now = item.next_attempt_at
    stats = deliver_due(FakeTransport(transient()), now)
    item.refresh_from_db()
    assert item.state == OutboxItem.FAILED and item.attempts == 5
    assert CouponDelivery.objects.for_org(org).count() == 0  # volta a ser elegível


def test_permanent_failure_marks_failed(org, destination, container_link):
    make_coupon("A")
    item = enqueue_coupon_notice(org, destination, NOW)
    deliver_due(FakeTransport(permanent()), NOW)
    item.refresh_from_db()
    assert item.state == OutboxItem.FAILED and item.attempts == 1


def test_timeout_is_uncertain_and_never_resent(org, destination, container_link):
    make_coupon("A")
    item = enqueue_coupon_notice(org, destination, NOW)
    transport = FakeTransport(uncertain())
    deliver_due(transport, NOW)
    item.refresh_from_db()
    assert item.state == OutboxItem.UNCERTAIN
    later = NOW + timedelta(hours=1)
    deliver_due(transport, later)
    assert len(transport.sent) == 1  # nenhum reenvio automático
    assert CouponDelivery.objects.for_org(org).count() == 1  # código segue marcado
    make_coupon("B")
    # destino destravado (sem pendente) mas o código A não volta
    again = enqueue_coupon_notice(org, destination, later)
    assert again and "*A*" not in again.body
