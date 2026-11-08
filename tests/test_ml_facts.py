from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.delivery.outbox import enqueue_for_rule
from apps.offers import catalog, live, manual, ml_api, niches
from apps.offers.models import AffiliateLink, Offer, Rule
from tests.conftest import NOW

pytestmark = pytest.mark.django_db

IMG = "https://http2.mlstatic.com/D_NQ_NP_1.webp"


class Http:
    """API do ML: o token e a lista de ofertas do produto (`/products/{id}/items`)."""

    def __init__(self, results):
        self.results = results

    def post(self, url, **kwargs):
        return SimpleNamespace(status_code=200, json=lambda: {"access_token": "t",
                                                                "expires_in": 21600})

    def get(self, url, **kwargs):
        return SimpleNamespace(status_code=200, json=lambda: {"results": self.results})


def row(price=499, original=849.86, tags=("kvs_primary",), shipping=None, official=None, **over):
    base = {"price": price, "original_price": original, "tags": list(tags), "item_id": "MLB1",
            "shipping": shipping, "official_store_id": official}
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def credentials(settings):
    settings.ML_API_CLIENT_ID, settings.ML_API_CLIENT_SECRET = "id", "secret"
    ml_api.reset_token_cache()
    yield
    ml_api.reset_token_cache()


def test_the_api_reports_free_shipping_and_the_official_store_of_the_winner():
    winner = row(shipping={"free_shipping": True, "mode": "me2"}, official=3401)
    other = row(price=520, tags=(), shipping={"free_shipping": False}, official=None)
    data = ml_api.product_price("MLB42225245", http=Http([other, winner]))
    assert (data["price"], data["free_shipping"], data["official_store"]) == (499.0, True, True)
    seller = ml_api.product_price("MLB1", http=Http([row(shipping={"free_shipping": False})]))
    assert (seller["free_shipping"], seller["official_store"]) == (False, False)


def test_what_the_api_does_not_say_stays_unknown_instead_of_false():
    data = ml_api.product_price("MLB1", http=Http([row(shipping=None)]))
    assert data["free_shipping"] is None and data["official_store"] is False
    assert ml_api.product_price("MLB1", http=Http([])) == {}


def test_the_live_proof_carries_the_facts(monkeypatch):
    offer = Offer(store="mercadolivre", product_key="MLB1", url="https://www.mercadolivre.com.br/p/MLB1",
                  price_from=Decimal(800), price_to=Decimal(500), discount_pct=Decimal("37.5"))
    monkeypatch.setattr(ml_api, "product_price", lambda pid, http=None: {
        "price": 500.0, "price_from": 800.0, "free_shipping": True, "official_store": True})
    proof, reason = live.validate(offer, 15)
    assert reason == "" and (proof.free_shipping, proof.official_store) == (True, True)
    monkeypatch.setattr(ml_api, "product_price", lambda pid, http=None: {
        "price": 500.0, "price_from": 800.0})
    proof, _reason = live.validate(offer, 15)
    assert (proof.free_shipping, proof.official_store) == (None, None)
    other = Offer(store="shopee", product_key="shp:1", url="https://shopee.com.br/product/1/2",
                  price_from=Decimal(800), price_to=Decimal(500), discount_pct=Decimal("37.5"))
    proof, _reason = live.validate(other, 15)
    assert (proof.free_shipping, proof.official_store) == (None, None)


@pytest.fixture
def rule(org, destination):
    return Rule.unscoped.create(org=org, destination=destination, name="Grupo", window_start=0,
                                window_end=0, niches=[])


def test_the_ml_message_says_what_the_listing_says_at_send_time(org, rule, monkeypatch):
    monkeypatch.setattr("apps.delivery.outbox.fetch_image_jpeg", lambda url: b"jpeg")
    monkeypatch.setattr(ml_api, "product_price", lambda pid, http=None: {
        "price": 500.0, "price_from": 1000.0, "free_shipping": True, "official_store": True})
    Offer.objects.create(
        store="mercadolivre", source="v1-catalogo", product_key="MLB1", link_key="MLB1",
        title="Air Fryer Oven 12L WAP", url="https://www.mercadolivre.com.br/p/MLB1",
        image_url=IMG, price_from=Decimal(1000), price_to=Decimal(500),
        discount_pct=Decimal(50), valid_on=NOW.date(), last_seen=NOW)
    AffiliateLink.unscoped.create(org=org, product_key="MLB1", affiliate_url="https://meli.la/a")
    item = enqueue_for_rule(rule, NOW)
    assert item.body == (
        "🛍️ *Air Fryer Oven 12L WAP*\n\nDe ❌ ~R$ 1.000~\nPor 🔥 *R$ 500* (50% OFF)\n"
        "🚚 Frete grátis · 🏪 Loja oficial\n\n🔗 https://meli.la/a")
    assert not Offer.objects.filter(product_key="MLB1").exclude(free_shipping=None).exists()


def test_the_catalog_collection_keeps_the_facts(org, rule, monkeypatch):
    sub = niches.sub_key("Eletrodomésticos", "Robô aspirador")
    Rule.unscoped.filter(id=rule.id).update(niches=[sub])
    monkeypatch.setattr(ml_api, "search_products", lambda term, limit=20, http=None: [
        {"id": "MLB900", "name": "Robô Aspirador X", "pictures": [{"url": IMG}]}])
    monkeypatch.setattr(ml_api, "product_price", lambda pid, http=None: {
        "price": 100.0, "price_from": 200.0, "winner_tagged": True, "free_shipping": True,
        "official_store": False})
    assert catalog.collect_catalog(NOW) >= 1
    offer = Offer.objects.get(product_key="MLB900")
    assert (offer.free_shipping, offer.official_store) == (True, False)


def test_a_pasted_ml_product_keeps_the_facts(org, monkeypatch):
    monkeypatch.setattr(ml_api, "product", lambda pid, http=None: {
        "name": "Air Fryer Oven 12L WAP", "image": IMG, "status": "active"})
    monkeypatch.setattr(ml_api, "product_price", lambda pid, http=None: {
        "price": 500.0, "price_from": 800.0, "free_shipping": True, "official_store": True})
    manual.add(org, "https://www.mercadolivre.com.br/p/MLB42225245", now=NOW)
    offer = Offer.objects.get(product_key="MLB42225245")
    assert (offer.free_shipping, offer.official_store) == (True, True)
