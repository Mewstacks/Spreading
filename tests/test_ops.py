from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.coupons.models import Coupon
from apps.delivery.health import run_canary, snapshot
from apps.delivery.management.commands.run_cycle import run_once
from apps.delivery.models import OutboxItem
from apps.delivery.outbox import deliver_due, enqueue_for_rule
from apps.delivery.transports import WahaTransport
from apps.offers.models import AffiliateLink, Rule
from tests.conftest import NOW, FakeTransport
from tests.test_feed import ROWS, page

OFFERS_HTML = """<script>const ITEMS = [{"vertical": "Casa", "data_oferta": "07/11/2026",
 "produto": "Jogo de Cama Casal", "url": "https://produto.mercadolivre.com.br/MLB-5256416008",
 "preco_de": "R$229,90", "preco_por": "R$149,90", "desconto": "35%"}];</script>"""


class Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._p = status, payload

    def json(self):
        if self._p is None:
            raise ValueError
        return self._p


class Http:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return self.resp


def test_waha_session_status_and_ack():
    http = Http(Resp(200, {"status": "WORKING"}))
    waha = WahaTransport("http://w", "k", "default", http=http)
    assert waha.session_status() == "WORKING" and waha.session_working()
    assert http.calls[0][0] == "http://w/api/sessions/default"
    http.resp = Resp(200, {"ack": 2})
    assert waha.message_ack("1@g.us", "true_1@g.us_A") == 2
    assert http.calls[-1][0] == "http://w/api/default/chats/1@g.us/messages/true_1@g.us_A"
    http.resp = Resp(404)
    assert waha.message_ack("c", "m") is None and waha.session_status() == ""


def test_waha_unreachable_is_not_working():
    import requests

    waha = WahaTransport("http://w", "k", "default",
                         http=Http(exc=requests.exceptions.ConnectionError()))
    assert waha.session_status() == "" and not waha.session_working()


def test_canary_ok_when_ack_reaches_device():
    transport = FakeTransport()
    transport.acks = {"true_c_1": 2}
    result = run_canary(transport, "c", wait_s=4, poll_s=1, sleep=lambda s: None)
    assert result["ok"] and result["ack"] == 2


def test_canary_fails_without_ack_or_chat():
    assert not run_canary(FakeTransport(), "")["ok"]
    result = run_canary(FakeTransport(), "c", wait_s=2, poll_s=1, sleep=lambda s: None)
    assert not result["ok"] and "ACK" in result["reason"]


def test_snapshot_reports_real_state(org, destination, container_link):
    Coupon.objects.create(code="A", source="t", valid_until=date(2026, 11, 15), last_seen=NOW,
                          container_url=container_link.container_url)
    transport = FakeTransport()
    data = snapshot(transport, NOW)
    assert data["whatsapp_session"] == "WORKING" and data["coupon_feed_fresh"] and data["ok"]
    transport.status = "FAILED"
    assert not snapshot(transport, NOW)["ok"]
    assert not snapshot(transport, NOW + timedelta(hours=5))["coupon_feed_fresh"]


def test_status_endpoint_requires_token(client, settings):
    settings.STATUS_TOKEN = "s3cret"
    assert client.get("/status").status_code == 403
    assert client.get("/status", headers={"X-Status-Token": "x"}).status_code == 403
    assert client.get("/healthz").status_code == 200


def test_run_cycle_end_to_end(org, destination, container_link):
    Rule.unscoped.create(org=org, destination=destination, name="Aviso", type=Rule.COUPON_NOTICE)
    offers_rule = Rule.unscoped.create(org=org, destination=destination, name="Ofertas")
    AffiliateLink.unscoped.create(org=org, product_key="MLB5256416008",
                                  affiliate_url="https://meli.la/cama")
    transport = FakeTransport()
    report = run_once(transport, NOW, coupon_html=page(ROWS), offers_html=OFFERS_HTML)
    assert report["cupons"] == 2 and report["ofertas_dia"] == 1
    assert report["enfileirados"] == 1  # 1 pendente por destino
    assert report["entrega"]["confirmado"] == 1
    assert len(transport.sent) == 1
    second = run_once(transport, NOW + timedelta(minutes=2), coupon_html=page(ROWS),
                      offers_html=OFFERS_HTML)
    assert second["entrega"]["confirmado"] <= 1 and len(transport.sent) <= 2
    assert OutboxItem.objects.for_org(org).filter(state=OutboxItem.CONFIRMED).count() >= 1
    assert offers_rule.id


def test_run_cycle_survives_bad_source_and_skips_when_session_down(org, destination):
    def boom():
        raise RuntimeError("feed fora")

    import apps.delivery.management.commands.run_cycle as cycle

    transport = FakeTransport()
    transport.status = "SCAN_QR_CODE"
    original = cycle.fetch_feed
    cycle.fetch_feed = boom
    try:
        report = run_once(transport, NOW, offers_html="")
    finally:
        cycle.fetch_feed = original
    assert report["cupons"].startswith("erro")
    assert "fora de WORKING" in report["entrega"] and transport.sent == []


@pytest.mark.parametrize("state", ["queued"])
def test_session_down_keeps_queue_untouched(org, destination, container_link, state):
    Coupon.objects.create(code="A", source="t", valid_until=date(2026, 11, 15), last_seen=NOW,
                          discount_pct=Decimal(30), container_url=container_link.container_url)
    rule = Rule.unscoped.create(org=org, destination=destination, name="Aviso",
                                type=Rule.COUPON_NOTICE)
    assert enqueue_for_rule(rule, NOW)
    transport = FakeTransport()
    transport.status = "FAILED"
    report = run_once(transport, NOW, coupon_html="", offers_html="")
    assert "fora de WORKING" in report["entrega"]
    assert OutboxItem.objects.for_org(org).get().state == state
    assert deliver_due(FakeTransport(), NOW)["confirmado"] == 1
