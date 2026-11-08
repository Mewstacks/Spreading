import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.offers import shopee, shopee_report
from apps.offers.models import Conversion, Rule, TermRun
from apps.orgs.models import Organization
from tests.conftest import NOW

pytestmark = pytest.mark.django_db

END = {"hasNextPage": False, "scrollId": ""}


def item(name="Suporte de mesa universal", qty=3, amount="14.97", price="4.99"):
    return {"itemId": 7, "modelId": 8, "itemName": name, "itemPrice": price, "qty": qty,
            "actualAmount": amount, "refundAmount": "0", "itemTotalCommission": "0.4491",
            "fraudStatus": "VERIFIED", "displayItemStatus": "COMPLETED"}


def node(conversion="111", order="O1", status="COMPLETED", net="1.5000", items=None, when=NOW,
         sub="----", channel="WhatsApp"):
    return {
        "conversionId": conversion, "checkoutId": conversion, "purchaseTime": int(when.timestamp()),
        "conversionStatus": status, "totalCommission": net, "netCommission": net,
        "estimatedTotalCommission": net, "utmContent": sub, "referrer": channel,
        "orders": [{"orderId": order, "orderStatus": status, "items": items or [item()]}],
    }


class Http:
    """Páginas do conversionReport em fila; guarda cada consulta feita."""

    def __init__(self, *pages, status=200):
        self.pages, self.status, self.queries = list(pages), status, []

    def post(self, url, data=None, headers=None, timeout=None):
        self.queries.append(json.loads(data)["query"])
        page = self.pages.pop(0) if self.pages else {"nodes": [], "pageInfo": END}
        return SimpleNamespace(status_code=self.status,
                               json=lambda: {"data": {"conversionReport": page}})


def page(*nodes, more=False, scroll=""):
    return {"nodes": list(nodes), "pageInfo": {"hasNextPage": more, "scrollId": scroll}}


@pytest.fixture(autouse=True)
def creds(settings):
    settings.SHOPEE_APP_ID = "app123"
    settings.SHOPEE_APP_SECRET = "secret456"


@pytest.fixture
def owner(org, destination):
    Rule.unscoped.create(org=org, destination=destination, name="Grupo", stores="shopee")
    return org


def test_one_row_per_order_with_the_net_commission(owner):
    http = Http(page(
        node("111", "O1", "COMPLETED", "1.5000"),
        node("222", "O2", "PENDING", "0.4491", items=[item("Lixeira"), item("Kit", 1, "20.00")]),
        node("333", "O3", "CANCELLED", "0", channel="YouTube"),
    ))
    assert shopee_report.sync(NOW, http=http) == {"vendas": 3}
    done = Conversion.unscoped.get(external_id="111:O1")
    assert (done.org, done.store, done.status) == (owner, "shopee", "COMPLETED")
    assert (done.commission, done.amount, done.items) == (Decimal("1.5"), Decimal("14.97"), 1)
    assert (done.title, done.channel, done.sub_id) == (
        "Suporte de mesa universal", "WhatsApp", "----")
    assert done.purchased_at == NOW
    two = Conversion.unscoped.get(external_id="222:O2")
    assert (two.title, two.items, two.amount) == ("Lixeira", 2, Decimal("34.97"))
    assert Conversion.unscoped.get(external_id="333:O3").channel == "YouTube"


def test_the_first_load_looks_back_ninety_days_and_the_next_ones_sixty(owner):
    http = Http(page(node()), page())
    shopee_report.sync(NOW, http=http)
    first = int((NOW - timedelta(days=90)).timestamp())
    assert f"purchaseTimeStart: {first}" in http.queries[0]
    assert f"purchaseTimeEnd: {int(NOW.timestamp())}" in http.queries[0]
    assert "scrollId:" not in http.queries[0]  # sem cursor na primeira página
    later = NOW + timedelta(hours=2)
    shopee_report.sync(later, http=http)
    assert f"purchaseTimeStart: {int((later - timedelta(days=60)).timestamp())}" in http.queries[1]


def test_it_follows_the_scroll_id_until_the_last_page(owner):
    http = Http(page(node("1", "A"), more=True, scroll="S1"),
                page(node("2", "B"), more=True, scroll="S2"), page(node("3", "C")))
    assert shopee_report.sync(NOW, http=http) == {"vendas": 3}
    assert len(http.queries) == 3
    assert 'scrollId: "S1"' in http.queries[1] and 'scrollId: "S2"' in http.queries[2]


def test_a_later_sync_updates_the_status_in_place_once_an_hour(owner):
    http = Http(page(node("1", "A", "PENDING", "2.0000")),
                page(node("1", "A", "COMPLETED", "2.0000")))
    shopee_report.sync(NOW, http=http)
    assert shopee_report.sync(NOW + timedelta(minutes=30), http=http) == {}
    assert len(http.queries) == 1
    assert shopee_report.sync(NOW + timedelta(minutes=61), http=http) == {"vendas": 1}
    assert Conversion.unscoped.count() == 1
    assert Conversion.unscoped.get().status == "COMPLETED"


def test_a_failure_raises_for_the_source_state_and_waits_an_hour(owner):
    http = Http(status=503)
    with pytest.raises(shopee.ShopeeError):
        shopee_report.sync(NOW, http=http)
    assert TermRun.objects.filter(term=shopee_report.RUN_KEY).exists()
    assert shopee_report.sync(NOW + timedelta(minutes=10), http=http) == {}
    assert len(http.queries) == 1


def test_broken_nodes_are_skipped_without_losing_the_good_ones(owner):
    broken = node("2", "B", net="não é número")
    no_order = {**node("3", "C"), "orders": []}
    long_title = node("4", "D", items=[item("x" * 500)])
    http = Http(page(node("1", "A"), broken, no_order, long_title, {"conversionId": "5"}))
    assert shopee_report.sync(NOW, http=http) == {"vendas": 2}
    assert set(Conversion.unscoped.values_list("external_id", flat=True)) == {"1:A", "4:D"}
    assert len(Conversion.unscoped.get(external_id="4:D").title) == 300


def test_nothing_without_credentials_or_an_owner(settings, org):
    http = Http(page(node()))
    assert shopee_report.sync(NOW, http=http) == {"vendas": 1}  # sem regra: cai na primeira org
    assert Conversion.unscoped.count() == 1
    Conversion.unscoped.all().delete()
    TermRun.objects.all().delete()
    settings.SHOPEE_APP_SECRET = ""
    assert shopee_report.sync(NOW, http=http) == {} and len(http.queries) == 1
    settings.SHOPEE_APP_SECRET = "secret456"
    Organization.objects.all().delete()
    assert shopee_report.sync(NOW, http=http) == {} and len(http.queries) == 1


def test_the_sales_belong_to_the_org_that_has_an_active_shopee_rule(org, destination):
    other = Organization.objects.create(name="Outra", slug="outra")
    assert other.id > org.id
    Rule.unscoped.create(org=org, destination=destination, name="G", stores="mercadolivre")
    Rule.unscoped.create(org=other, destination=destination, name="S", stores="shopee")
    shopee_report.sync(NOW, http=Http(page(node())))
    assert Conversion.unscoped.get().org == other  # a primeira org não tem Shopee ativa
