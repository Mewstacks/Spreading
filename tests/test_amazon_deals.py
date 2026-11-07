import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import requests

from apps.delivery.outbox import enqueue_for_rule
from apps.offers import amazon, amazon_deals
from apps.offers.models import Offer, Rule, TermRun
from tests.conftest import NOW


def deal(asin="B0F4SLVB9Y", title="Omega 3 TG Líquido 150ml Frutas", price="153.0", basis="177.0",
         final="120.0", rating="4,6", reviews=165):
    return {
        "asin": asin, "title": title, "link": f"/x/dp/{asin}",
        "image": {"hiRes": {"baseUrl": "https://m.media-amazon.com/images/I/515x0-9Lx5L",
                            "extension": "jpg"}},
        "price": {"priceToPay": {"label": "Por:", "price": price},
                  "basisPrice": {"label": "De:", "price": basis, "strikethrough": True}},
        "coupon": {"label": {"fragments": ["Você paga ", {"money": {"amount": final}}]},
                   "messaging": {"text": " com o cupom"}, "id": "/promo/A1LKAWBXRREYGJ"},
        "customerReviews": {"count": {"value": reviews}, "rating": {"shortDisplayString": rating}},
    }


def page(*deals, extra=""):
    blob = ",".join(json.dumps(d, ensure_ascii=False) for d in deals)
    return f"<html><script>window.__STATE__ = {{\"items\":[{blob}]}};{extra}</script></html>"


class Session:
    def __init__(self, html="", status=200, error=None):
        self.html, self.status, self.error, self.calls = html, status, error, 0

    def get(self, url, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.status, text=self.html)


@pytest.fixture
def rule(org, destination):
    org.amazon_tag = "luizahfn-20"
    org.save()
    return Rule.unscoped.create(org=org, destination=destination, name="G", window_start=0,
                                window_end=0, stores="amazon", niches=[])


def test_parse_reads_final_price_list_price_photo_and_rating_from_the_embedded_json():
    [row] = amazon_deals.parse(page(deal()))
    assert row["asin"] == "B0F4SLVB9Y" and row["final"] == Decimal("120.00")
    assert (row["price"], row["basis"]) == (Decimal("153.00"), Decimal("177.00"))
    assert row["image"] == "https://m.media-amazon.com/images/I/515x0-9Lx5L.jpg"
    assert row["rating"] == Decimal("4.6") and row["reviews"] == 165


def test_parse_ignores_deals_without_a_coupon_and_duplicates():
    plain = deal(asin="B000000001")
    plain.pop("coupon")
    html = page(plain, deal(asin="B000000002"), deal(asin="B000000002"))
    assert [d["asin"] for d in amazon_deals.parse(html)] == ["B000000002"]


def test_parse_survives_braces_in_titles_and_broken_fragments():
    html = page(deal(asin="B000000003", title='Kit {3 peças} "premium"'))
    html += '{"asin":"B000000004","title":"quebrado'  # JSON truncado no fim da página
    assert [d["asin"] for d in amazon_deals.parse(html)] == ["B000000003"]


def test_vet_gates_rating_reviews_discount_and_category():
    good = amazon_deals.parse(page(deal()))[0]
    vetted = amazon_deals.vet(good)
    assert vetted["base"] == Decimal("177.00") and vetted["pct"] == Decimal("32.20")
    assert vetted["coupon_pct"] == Decimal("21.6")  # 153 -> 120
    for over in [{"rating": Decimal("3.5")}, {"rating": None}, {"reviews": 3},
                 {"final": Decimal("153.00")}, {"final": Decimal("40.00")},  # 77%: fachada
                 {"title": "Remédio emagrecedor"}, {"image": "https://evil.io/a.jpg"}]:
        assert amazon_deals.vet({**good, **over}) is None


def test_collect_stores_coupon_pairs_that_reach_the_group_with_the_tag(org, rule, monkeypatch):
    monkeypatch.setattr("apps.delivery.outbox.fetch_image_jpeg", lambda url: b"jpeg")
    http = Session(page(deal(), deal(asin="B000000009", rating="3,0")))
    assert amazon_deals.collect(NOW, http=http) == 1
    offer = Offer.objects.get()
    assert (offer.source, offer.coupon_kind, offer.coupon_pct) == (
        "amazon-cupons", "activation", Decimal("21.6"))
    assert (offer.price_from, offer.price_to) == (Decimal("177.00"), Decimal("120.00"))
    item = enqueue_for_rule(rule, NOW)
    assert "🎟️ CUPOM: ative na página" in item.body and "Por 🔥 *R$ 120*" in item.body
    assert "https://www.amazon.com.br/dp/B0F4SLVB9Y?tag=luizahfn-20" in item.body


def test_collect_runs_once_an_hour_and_only_for_amazon_rules_with_the_tag(org, rule):
    http = Session(page(deal()))
    amazon_deals.collect(NOW, http=http)
    amazon_deals.collect(NOW + timedelta(minutes=30), http=http)
    assert http.calls == 1
    amazon_deals.collect(NOW + timedelta(minutes=61), http=http)
    assert http.calls == 2
    rule.stores = "mercadolivre"
    rule.save()
    amazon_deals.collect(NOW + timedelta(hours=3), http=http)
    assert http.calls == 2  # sem regra com Amazon, sem consulta
    rule.stores = "amazon"
    rule.save()
    org.amazon_tag = ""
    org.save()
    amazon_deals.collect(NOW + timedelta(hours=5), http=http)
    assert http.calls == 2  # sem a tag do dono, sem link, sem coleta


@pytest.mark.parametrize("http", [Session(status=503), Session("captcha required"),
                                  Session(error=requests.exceptions.ConnectTimeout())])
def test_a_block_pauses_the_whole_amazon_store_for_an_hour(org, rule, http):
    amazon_deals.collect(NOW, http=Session(page(deal())))
    assert Offer.objects.count() == 1
    later = NOW + timedelta(hours=2)
    assert amazon_deals.collect(later, http=http) == 0
    assert Offer.objects.count() == 1 and TermRun.objects.filter(term=amazon.BLOCK_KEY).exists()
    ok = Session(page(deal(asin="B000000007")))
    amazon_deals.collect(later + timedelta(minutes=30), http=ok)
    assert ok.calls == 0
    amazon_deals.collect(later + timedelta(minutes=61), http=ok)
    assert ok.calls == 1


def test_switch_off(org, rule, settings):
    settings.AMAZON_COLLECT = False
    http = Session(page(deal()))
    assert amazon_deals.collect(NOW, http=http) == 0 and http.calls == 0
