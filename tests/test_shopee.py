import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import requests

from apps.delivery.outbox import enqueue_for_rule
from apps.offers import niches, shopee
from apps.offers.models import AffiliateLink, Offer, Rule, TermRun
from tests.conftest import NOW

GOOD = {
    "itemId": "9001", "productName": "Fone Bluetooth TWS Pro com Cancelamento de Ruído",
    "productLink": "https://shopee.com.br/product/111/9001",
    "offerLink": "https://s.shopee.com.br/AbCd123",
    "imageUrl": "https://down-br.img.susercontent.com/file/br-11134207-abc",
    "priceMin": "70.00", "priceMax": "70.00", "priceDiscountRate": 30,
    "commissionRate": "0.08", "sales": 540, "ratingStar": "4.9", "shopId": "111",
    "shopName": "Loja Boa",
}


class Http:
    def __init__(self, nodes=None, status=200, body=None, error=None):
        self.nodes, self.status, self.body, self.error = nodes or [], status, body, error
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(SimpleNamespace(url=url, data=data, headers=headers))
        if self.error:
            raise self.error
        page = {"page": 1, "limit": 50, "hasNextPage": False}
        payload = self.body if self.body is not None else {
            "data": {"productOfferV2": {"nodes": self.nodes, "pageInfo": page}}}
        return SimpleNamespace(status_code=self.status, json=lambda: payload)


@pytest.fixture(autouse=True)
def creds(settings):
    settings.SHOPEE_APP_ID = "app123"
    settings.SHOPEE_APP_SECRET = "secret456"


@pytest.fixture
def rule(org, destination):
    return Rule.unscoped.create(
        org=org, destination=destination, name="Grupo", window_start=0, window_end=0,
        stores="shopee", niches=[niches.sub_key("Celulares, Telefonia e Wearables",
                                                "Fones bluetooth")],
    )


# ── cliente ───────────────────────────────────────────────────────────────
def test_request_is_signed_over_the_exact_bytes_that_travel(monkeypatch):
    monkeypatch.setattr(shopee, "time", SimpleNamespace(time=lambda: 1_700_000_000))
    http = Http()
    shopee.execute("query { x }", {"a": 1}, app_id="app123", secret="secret456", http=http)
    call = http.calls[0]
    body = call.data.decode("utf-8")
    assert json.loads(body) == {"query": "query { x }", "variables": {"a": 1}}
    expected = hashlib.sha256(f"app1231700000000{body}secret456".encode()).hexdigest()
    assert call.headers["Authorization"] == (
        f"SHA256 Credential=app123, Timestamp=1700000000, Signature={expected}")
    assert "secret456" not in call.headers["Authorization"] and "secret456" not in body


@pytest.mark.parametrize("http, retryable", [
    (Http(status=429), True), (Http(status=503), True),
    (Http(error=requests.exceptions.Timeout()), True),
    (Http(body={"errors": [{"message": "rate", "extensions": {"code": 10030}}]}), True),
    (Http(body={"errors": [{"message": "Invalid Signature", "extensions": {"code": 10020}}]}),
     False),
    (Http(status=400, body={"data": {}}), False),
])
def test_failures_are_typed_and_never_look_like_an_empty_collection(http, retryable):
    with pytest.raises(shopee.ShopeeError) as caught:
        shopee.execute("q", app_id="a", secret="s", http=http)
    assert caught.value.retryable is retryable


def test_missing_credentials_never_reach_the_network():
    http = Http()
    with pytest.raises(shopee.ShopeeConfigError):
        shopee.execute("q", app_id="", secret="s", http=http)
    assert http.calls == []


# ── coleta ────────────────────────────────────────────────────────────────
def test_collect_stores_vetted_offers_with_the_official_link(org, rule):
    junk = [
        {**GOOD, "itemId": "1", "ratingStar": "3.9"},  # nota baixa
        {**GOOD, "itemId": "2", "sales": 3},  # ninguém comprou
        {**GOOD, "itemId": "3", "priceDiscountRate": 0},  # sem desconto
        {**GOOD, "itemId": "4", "priceDiscountRate": 85},  # desconto de fachada
        {**GOOD, "itemId": "5", "offerLink": "https://evil.io/x"},  # link fora da Shopee
        {**GOOD, "itemId": "6", "imageUrl": "https://evil.io/a.jpg"},  # foto fora da lista
        {**GOOD, "itemId": "7", "priceMin": "0"},
        {**GOOD, "itemId": "8", "productName": "Remédio emagrecedor"},  # categoria vedada
    ]
    http = Http([GOOD, *junk])
    assert shopee.collect(NOW, http=http) == 1
    offer = Offer.objects.get()
    assert (offer.store, offer.link_key, offer.source) == ("shopee", "shp:9001", "shopee-api")
    assert offer.price_to == Decimal("70.00") and offer.price_from == Decimal("100.00")
    assert offer.discount_pct == Decimal("30.00") and offer.macro
    link = AffiliateLink.objects.for_org(org).get()
    assert (link.product_key, link.affiliate_url) == ("shp:9001", GOOD["offerLink"])


def test_collect_asks_for_the_rule_terms_once_an_hour_and_only_for_shopee_rules(
        org, rule, destination):
    http = Http([GOOD])
    shopee.collect(NOW, http=http)
    sent = [json.loads(c.data)["variables"]["keyword"] for c in http.calls]
    assert set(sent) == {t.strip() for t in niches._sub_terms(rule.niches[0])}
    asked = len(http.calls)
    shopee.collect(NOW + timedelta(minutes=30), http=http)
    assert len(http.calls) == asked  # o ciclo roda de 5 em 5 minutos
    shopee.collect(NOW + timedelta(minutes=61), http=http)
    assert len(http.calls) == 2 * asked
    assert TermRun.objects.filter(term__startswith="shopee:").count() == asked

    rule.stores = "mercadolivre"
    rule.save()
    before = len(http.calls)
    shopee.collect(NOW + timedelta(hours=3), http=http)
    assert len(http.calls) == before  # regra sem Shopee não gera consulta


def test_rule_without_niches_asks_the_general_listing(org, rule):
    rule.niches = []
    rule.save()
    http = Http([GOOD])
    shopee.collect(NOW, http=http)
    assert [json.loads(c.data)["variables"]["keyword"] for c in http.calls] == [None]


def test_macro_selection_uses_its_main_keywords(org, rule):
    rule.niches = [niches.macro_key("Pets e Animais")]
    rule.save()
    http = Http([])
    shopee.collect(NOW, http=http)
    asked = [json.loads(c.data)["variables"]["keyword"] for c in http.calls]
    assert asked and set(asked) <= {"racao", "ração", "petisco", "coleira"}


def test_api_failure_keeps_what_was_collected_and_retries_the_term(org, rule):
    shopee.collect(NOW, http=Http([GOOD]))
    assert Offer.objects.count() == 1
    count = shopee.collect(NOW + timedelta(hours=2), http=Http(status=503))
    assert count == 0 and Offer.objects.count() == 1  # fonte quebrada não apaga catálogo
    retry = Http([GOOD])
    shopee.collect(NOW + timedelta(hours=2, minutes=5), http=retry)
    assert retry.calls  # o termo que falhou não ficou marcado como coletado


def test_not_configured_collects_nothing(org, rule, settings):
    settings.SHOPEE_APP_ID = ""
    http = Http([GOOD])
    assert shopee.collect(NOW, http=http) == 0 and http.calls == []


# ── ponta a ponta: da API ao texto do grupo ───────────────────────────────
def test_a_shopee_offer_reaches_the_group_in_the_golden_format(org, rule, monkeypatch):
    monkeypatch.setattr("apps.delivery.outbox.fetch_image_jpeg", lambda url: b"jpeg")
    shopee.collect(NOW, http=Http([GOOD]))
    item = enqueue_for_rule(rule, NOW)
    assert item and item.image_url == GOOD["imageUrl"]
    assert item.body == (
        "🛍️ *Fone Bluetooth TWS Pro com Cancelamento de Ruído*\n\n"
        "De ❌ ~R$ 100~\nPor 🔥 *R$ 70*\n\n🔗 https://s.shopee.com.br/AbCd123"
    )
