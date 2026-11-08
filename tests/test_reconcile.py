from datetime import timedelta
from types import SimpleNamespace

import pytest
import requests

from apps.delivery import reconcile
from apps.delivery.models import CouponDelivery, OfferDelivery, OutboxItem
from apps.delivery.transports import SessionRouter, WahaTransport
from tests.conftest import NOW, FakeTransport
from tests.test_sessions import Sessions, group

pytestmark = pytest.mark.django_db

BODY = "🛍️ *Air Fryer*\n\nDe ❌ ~R$ 800~\nPor 🔥 *R$ 500* (38% OFF)\n\n🔗 https://meli.la/abc"


class History(FakeTransport):
    """Transporte falso com o histórico do chat; `history=None` é o WAHA fora do ar."""

    def __init__(self, history=()):
        super().__init__()
        self.history = None if history is None else list(history)
        self.asked = []

    def recent_messages(self, chat_id, limit=50):
        self.asked.append((chat_id, limit))
        return self.history


def msg(body=BODY, at=NOW, from_me=True, ident="true_c_ABC"):
    return {"id": ident, "from_me": from_me, "ts": int(at.timestamp()), "body": body, "ack": 3}


def uncertain(org, destination, body=BODY, age=timedelta(minutes=15), key="k1"):
    stamp = NOW - age
    item = OutboxItem.unscoped.create(
        org=org, destination=destination, kind="offer", body=body, idempotency_key=key,
        state=OutboxItem.UNCERTAIN, next_attempt_at=stamp, created_at=stamp, last_error="timeout")
    OfferDelivery.unscoped.create(org=org, destination=destination, product_key="MLB1",
                                  identity="air fryer", outbox_item=item, created_at=stamp)
    return item


def state(item):
    item.refresh_from_db()
    return item.state


def test_a_message_found_in_the_chat_confirms_the_item(org, destination):
    item = uncertain(org, destination)
    chat = History([msg(at=NOW - timedelta(minutes=14), ident="true_c_FOUND")])
    assert reconcile.run(chat, NOW) == {"confirmado": 1, "falhou": 0, "pendente": 0}
    item.refresh_from_db()
    assert item.state == OutboxItem.CONFIRMED and item.external_message_id == "true_c_FOUND"
    assert item.sent_at == NOW - timedelta(minutes=14) and item.last_error == "reconciliado"
    assert chat.asked == [(destination.external_id, reconcile.FETCH)]
    assert OfferDelivery.unscoped.count() == 1  # o cooldown continua: entregou
    assert reconcile.run(chat, NOW) == {"confirmado": 0, "falhou": 0, "pendente": 0}  # nada mais


def test_the_match_ignores_spacing_and_other_senders_and_older_messages(org, destination):
    item = uncertain(org, destination, age=timedelta(minutes=5))  # novo: ainda não dá por perdido
    other_body = "🛍️ *Air Fryer*\n\nDe ❌ ~R$ 900~\nPor 🔥 *R$ 450*\n\n🔗 https://meli.la/zzz"
    chat = History([
        msg(other_body, NOW - timedelta(minutes=4)),  # outro texto
        msg(BODY, NOW - timedelta(minutes=4), from_me=False),  # de outra pessoa
        msg(BODY, NOW - timedelta(hours=3)),  # igual, mas de antes do envio
    ])
    assert reconcile.run(chat, NOW) == {"confirmado": 0, "falhou": 0, "pendente": 1}
    spaced = "  " + BODY.replace("\n\n", "\n \n") + "  "  # mesma legenda, outro espaçamento
    chat.history.append(msg(spaced, NOW - timedelta(minutes=3)))
    assert reconcile.run(chat, NOW)["confirmado"] == 1 and state(item) == OutboxItem.CONFIRMED


def test_a_message_that_never_arrived_fails_the_item_and_frees_the_product(org, destination):
    item = uncertain(org, destination)
    coupon_item = uncertain(org, destination, key="k2")
    # a lista cobre o horário do envio (há mensagem de antes) e a nossa não está nela
    chat = History([msg("bom dia, pessoal", NOW - timedelta(hours=2), ident="x1")])
    assert reconcile.run(chat, NOW) == {"confirmado": 0, "falhou": 2, "pendente": 0}
    assert state(item) == state(coupon_item) == OutboxItem.FAILED
    item.refresh_from_db()
    assert item.last_error == "nao_chegou (reconciliado)"
    assert not OfferDelivery.unscoped.filter(outbox_item=item).exists()  # pode voltar a sair


def test_a_short_history_counts_as_the_whole_history(org, destination):
    item = uncertain(org, destination)
    assert reconcile.run(History([]), NOW)["falhou"] == 1  # chat sem nada além do que existe
    assert state(item) == OutboxItem.FAILED


def test_nothing_is_decided_when_the_list_does_not_reach_back_to_the_send(org, destination):
    item = uncertain(org, destination)
    # o chat movimentado devolve só as últimas FETCH mensagens, todas depois do envio
    chat = History([msg("papo", NOW - timedelta(minutes=5), ident=f"n{n}")
                    for n in range(reconcile.FETCH)])
    assert reconcile.run(chat, NOW) == {"confirmado": 0, "falhou": 0, "pendente": 1}
    assert state(item) == OutboxItem.UNCERTAIN


def test_when_the_waha_does_not_answer_the_item_waits(org, destination):
    item = uncertain(org, destination)
    assert reconcile.run(History(None), NOW) == {"confirmado": 0, "falhou": 0, "pendente": 1}
    assert state(item) == OutboxItem.UNCERTAIN


def test_a_young_item_is_not_judged_yet_and_it_can_still_be_confirmed(org, destination):
    item = uncertain(org, destination, age=timedelta(minutes=5))
    gone = History([msg("papo", NOW - timedelta(hours=1))])
    assert reconcile.run(gone, NOW) == {"confirmado": 0, "falhou": 0, "pendente": 1}  # < 10 min
    arrived = History([msg(BODY, NOW - timedelta(minutes=4))])
    assert reconcile.run(arrived, NOW)["confirmado"] == 1 and state(item) == OutboxItem.CONFIRMED
    assert reconcile.run(History([]), NOW) == {"confirmado": 0, "falhou": 0, "pendente": 0}
    tiny = uncertain(org, destination, age=timedelta(seconds=30), key="k3")
    assert reconcile.run(History([]), NOW)["pendente"] == 1 and state(tiny) == OutboxItem.UNCERTAIN


def test_only_recent_uncertain_items_are_looked_at_and_at_most_five_per_cycle(org, destination):
    old = uncertain(org, destination, age=timedelta(hours=49), key="old")
    for number in range(7):
        uncertain(org, destination, key=f"n{number}")
    chat = History([msg("papo", NOW - timedelta(hours=60))])
    stats = reconcile.run(chat, NOW)
    assert stats["falhou"] == reconcile.PER_CYCLE and len(chat.asked) == reconcile.PER_CYCLE
    assert state(old) == OutboxItem.UNCERTAIN  # fora da janela de 48 h


def test_the_failed_coupon_notice_frees_its_codes_too(org, destination):
    from apps.coupons.models import Coupon

    item = uncertain(org, destination)
    coupon = Coupon.objects.create(code="VALE10", valid_until=NOW.date(), last_seen=NOW)
    CouponDelivery.unscoped.create(org=org, destination=destination, coupon=coupon,
                                   outbox_item=item)
    reconcile.run(History([msg("papo", NOW - timedelta(hours=2))]), NOW)
    assert not CouponDelivery.unscoped.exists()


def test_each_item_is_checked_through_its_own_number(org):
    first, second = group(org, "a@g.us"), group(org, "b@g.us", "numero2")
    a, b = uncertain(org, first, key="a"), uncertain(org, second, key="b")
    router = SessionRouter(Sessions({"default", "numero2"}))
    default, other = router.for_session("default"), router.for_session("numero2")
    default.recent_messages = lambda chat, limit=50: [msg(BODY, NOW - timedelta(minutes=14),
                                                          ident="d1")]
    other.recent_messages = lambda chat, limit=50: [msg("papo", NOW - timedelta(hours=2))]
    assert reconcile.run(router, NOW) == {"confirmado": 1, "falhou": 1, "pendente": 0}
    assert state(a) == OutboxItem.CONFIRMED and state(b) == OutboxItem.FAILED


def test_a_number_that_is_down_is_not_asked(org, destination):
    item = uncertain(org, destination)
    chat = History([])
    chat.status = "FAILED"
    assert reconcile.run(chat, NOW) == {"confirmado": 0, "falhou": 0, "pendente": 1}
    assert chat.asked == [] and state(item) == OutboxItem.UNCERTAIN


def test_a_transport_without_history_does_nothing(org, destination):
    item = uncertain(org, destination)
    bare = SimpleNamespace(session_working=lambda: True)
    assert reconcile.run(bare, NOW) == {"confirmado": 0, "falhou": 0, "pendente": 1}
    assert state(item) == OutboxItem.UNCERTAIN


def test_nothing_uncertain_costs_nothing(org, destination):
    chat = History([])
    assert reconcile.run(chat, NOW) == {"confirmado": 0, "falhou": 0, "pendente": 0}
    assert chat.asked == []


# ── transporte: o que o WAHA devolve vira lista simples ──────────────────────
class Http:
    def __init__(self, payload=None, status=200, error=None):
        self.payload, self.status, self.error, self.calls = payload, status, error, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.status, json=lambda: self.payload)


def waha(http):
    return WahaTransport(base_url="http://waha:3000", api_key="k", session_name="default",
                         http=http)


def test_recent_messages_reads_the_chat_history_from_the_waha():
    http = Http([
        {"id": "true_c_A", "fromMe": True, "timestamp": 1794162025, "body": "oi", "ack": 3,
         "hasMedia": True},
        {"id": "false_c_B", "fromMe": False, "timestamp": 1794112463, "body": None, "ack": 1},
        {"fromMe": True}, "lixo", None])
    rows = waha(http).recent_messages("c@g.us", 30)
    assert rows == [
        {"id": "true_c_A", "from_me": True, "ts": 1794162025, "body": "oi", "ack": 3},
        {"id": "false_c_B", "from_me": False, "ts": 1794112463, "body": "", "ack": 1}]
    url, params, headers = http.calls[0]
    assert url == "http://waha:3000/api/default/chats/c@g.us/messages"
    assert params == {"limit": 30, "downloadMedia": "false"} and headers == {"X-Api-Key": "k"}


@pytest.mark.parametrize("http", [
    Http(status=500), Http(error=requests.exceptions.ConnectTimeout())])
def test_recent_messages_is_none_when_the_waha_cannot_answer(http):
    assert waha(http).recent_messages("c@g.us") is None


def test_a_payload_that_is_not_a_list_is_unreadable():
    assert waha(Http({"erro": "x"})).recent_messages("c@g.us") is None


def test_the_cycle_reconciles_before_it_queues_anything(org, destination):
    import apps.delivery.management.commands.run_cycle as cycle

    item = uncertain(org, destination)
    chat = History([msg(at=NOW - timedelta(minutes=14), ident="true_c_Z")])
    report = cycle.run_once(chat, NOW, coupon_html="[]", offers_html="[]")
    assert report["reconciliacao"] == {"confirmado": 1, "falhou": 0, "pendente": 0}
    assert state(item) == OutboxItem.CONFIRMED
