from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import requests

from apps.delivery.outbox import enqueue_for_rule
from apps.offers import amazon, niches
from apps.offers.models import Offer, Rule, TermRun
from tests.conftest import NOW


def coupon_html(final, label):
    return (
        '<span data-component-type="s-coupon-component" class="rush-component">'
        '<span class="s-coupon-clipped aok-block aok-hidden">'
        '<span class="s-coupon-highlight-color">'
        f'Você paga R$ {final}</span> <span class="a-color-base">{label} aplicado</span></span>'
        '<span class="s-coupon-unclipped aok-block">'
        '<span class="s-coupon-highlight-color">'
        f'Você paga R$ {final}</span> <span class="a-color-base"> com o cupom</span></span></span>'
    )


def card(asin="B0CH5PRZH9", title="Air Fryer Oven Digital 12L", price="542,71", struck="789,90",
         rating="4,6", image="https://m.media-amazon.com/images/I/51W._AC_UL320_.jpg", coupon=""):
    stars = f'<span class="a-icon-alt">{rating} de 5 estrelas</span>' if rating else ""
    return (
        f'<div role="listitem" data-asin="{asin}" data-component-type="s-search-result">'
        f'<img class="s-image" src="{image}"><h2 aria-label="Anúncio patrocinado - {title}">'
        f"<span>{title}</span></h2>{stars}"
        f'<span class="a-price" data-a-size="xl"><span class="a-offscreen">R$\xa0{price}</span>'
        f'<span aria-hidden="true">x</span></span>'
        + (f'<span class="a-price a-text-price" data-a-strike="true"><span class="a-offscreen">'
           f'R$\xa0{struck}</span></span>' if struck else "")
        + coupon + '</div>'
    )


def page(*cards):
    return "<html><body>" + "".join(cards) + "</body></html>"


class Http:
    def __init__(self, html="", status=200, error=None):
        self.html, self.status, self.error, self.calls = html, status, error, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(params["k"])
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.status, text=self.html)


@pytest.fixture
def rule(org, destination):
    org.amazon_tag = "luizahfn-20"
    org.save()
    return Rule.unscoped.create(
        org=org, destination=destination, name="G", window_start=0, window_end=0, stores="amazon",
        niches=[niches.sub_key("Eletrodomésticos", "Air fryer")])


def test_parse_reads_price_struck_price_rating_and_photo():
    [row] = amazon.parse(page(card(price="1.299,00", struck="1.599,90")))
    assert row["asin"] == "B0CH5PRZH9" and row["title"] == "Air Fryer Oven Digital 12L"
    assert (row["price"], row["struck"], row["rating"]) == (
        Decimal("1299.00"), Decimal("1599.90"), Decimal("4.6"))
    assert row["image"] == "https://m.media-amazon.com/images/I/51W.jpg"  # sem sufixo de miniatura


def test_parse_skips_cards_without_a_struck_price_or_photo():
    html = page(card(struck=""), card(asin="B000000002", image=""))
    assert amazon.parse(html) == []


def test_vet_gates_discount_rating_photo_and_category():
    good = amazon.parse(page(card()))[0]
    assert amazon.vet(good)["pct"] == Decimal("31.29")
    for over in [{"struck": Decimal("560.00")},  # 3%: quase nada
                 {"struck": Decimal("1500.00")},  # 64%: acima do teto
                 {"struck": Decimal("3000.00")},  # 82%: "De" de fachada
                 {"rating": Decimal("3.7")}, {"rating": None},
                 {"image": "https://evil.io/a.jpg"}, {"title": "Remédio emagrecedor 60 caps"}]:
        assert amazon.vet({**good, **over}) is None


def test_collect_stores_offers_that_reach_the_group_with_the_owner_tag(org, rule, monkeypatch):
    monkeypatch.setattr("apps.delivery.outbox.fetch_image_jpeg", lambda url: b"jpeg")
    html = page(card(), card(asin="B000000002", price="100,00", struck="105,00"))
    http = Http(html)
    assert amazon.collect(NOW, http=http, sleep=lambda s: None) == 1
    offer = Offer.objects.get()
    assert (offer.store, offer.link_key, offer.source) == ("amazon", "B0CH5PRZH9", "amazon-busca")
    assert offer.price_from == Decimal("789.90") and offer.price_to == Decimal("542.71")
    item = enqueue_for_rule(rule, NOW)
    assert item and "https://www.amazon.com.br/dp/B0CH5PRZH9?tag=luizahfn-20" in item.body
    assert "De ❌ ~R$ 789,90~" in item.body and "Por 🔥 *R$ 542,71*" in item.body


def test_collect_needs_the_owners_tag_and_an_amazon_rule(org, rule):
    org.amazon_tag = ""
    org.save()
    http = Http(page(card()))
    assert amazon.collect(NOW, http=http) == 0 and http.calls == []
    org.amazon_tag = "luizahfn-20"
    org.save()
    rule.stores = "mercadolivre"
    rule.save()
    assert amazon.collect(NOW, http=http) == 0 and http.calls == []


def test_each_term_once_an_hour_with_a_pause_between_queries(org, rule):
    sleeps, http = [], Http(page(card()))
    amazon.collect(NOW, http=http, sleep=sleeps.append)
    asked = len(http.calls)
    assert asked > 1 and len(sleeps) == asked - 1
    amazon.collect(NOW + timedelta(minutes=30), http=http, sleep=sleeps.append)
    assert len(http.calls) == asked
    amazon.collect(NOW + timedelta(minutes=61), http=http, sleep=sleeps.append)
    assert len(http.calls) == 2 * asked


@pytest.mark.parametrize("http", [
    Http(status=503), Http("<html>Digite os caracteres... captcha</html>"),
    Http(error=requests.exceptions.ConnectTimeout())])
def test_a_block_pauses_the_store_for_an_hour_and_keeps_the_catalog(org, rule, http):
    amazon.collect(NOW, http=Http(page(card())), sleep=lambda s: None)
    assert Offer.objects.count() == 1
    later = NOW + timedelta(hours=2)
    amazon.collect(later, http=http, sleep=lambda s: None)
    assert Offer.objects.count() == 1 and TermRun.objects.filter(term=amazon.BLOCK_KEY).exists()
    ok = Http(page(card()))
    amazon.collect(later + timedelta(minutes=30), http=ok)
    assert ok.calls == []  # pausada
    amazon.collect(later + timedelta(minutes=61), http=ok, sleep=lambda s: None)
    assert ok.calls


def test_switch_off(org, rule, settings):
    settings.AMAZON_COLLECT = False
    http = Http(page(card()))
    assert amazon.collect(NOW, http=http) == 0 and http.calls == []


# ── cupom no cartão ───────────────────────────────────────────────────────
def test_parse_reads_the_coupon_and_the_price_after_it():
    five = coupon_html("65,45", "Cupom de 5% de desconto")
    html = page(card(price="68,90", struck="", coupon=five))
    [row] = amazon.parse(html)
    assert row["coupon_final"] == Decimal("65.45") and row["coupon_pct"] == Decimal("5")
    assert row["coupon_amount"] is None and row["struck"] is None
    html = page(card(price="100,00", struck="150,00",
                     coupon=coupon_html("80,00", "Cupom de R$ 20 de desconto")))
    [row] = amazon.parse(html)
    assert row["coupon_amount"] == Decimal("20") and row["coupon_pct"] is None


def test_vet_uses_the_coupon_price_and_the_pre_coupon_price_as_base():
    [row] = amazon.parse(page(card(price="100,00", struck="",
                                   coupon=coupon_html("70,00", "Cupom de 30% de desconto"))))
    vetted = amazon.vet(row)
    assert vetted["coupon"]
    assert (vetted["base"], vetted["final"]) == (Decimal("100.00"), Decimal("70.00"))
    assert vetted["pct"] == Decimal("30.00")
    [row] = amazon.parse(page(card(price="100,00", struck="120,00",
                                   coupon=coupon_html("70,00", "Cupom de 30% de desconto"))))
    assert amazon.vet(row)["base"] == Decimal("120.00")  # o riscado do anúncio é o "De"
    # um "Você paga" que não é menor que o preço não é cupom
    [row] = amazon.parse(page(card(price="100,00", struck="130,00",
                                   coupon=coupon_html("100,00", "Cupom de 1% de desconto"))))
    assert amazon.vet(row)["coupon"] is False


def test_the_listing_rating_is_kept_for_the_message(org, rule):
    html = page(card(rating="4,6"), card(asin="B000000002", rating="4,0"))
    assert amazon.collect(NOW, http=Http(html), sleep=lambda s: None) == 2
    assert dict(Offer.objects.values_list("product_key", "rating")) == {
        "B0CH5PRZH9": Decimal("4.6"), "B000000002": Decimal("4.0")}


def test_coupon_card_becomes_an_activation_pair_in_the_golden_message(org, rule, monkeypatch):
    monkeypatch.setattr("apps.delivery.outbox.fetch_image_jpeg", lambda url: b"jpeg")
    thirty = coupon_html("70,00", "Cupom de 30% de desconto")
    html = page(card(price="100,00", struck="", coupon=thirty))
    assert amazon.collect(NOW, http=Http(html), sleep=lambda s: None) == 1
    offer = Offer.objects.get()
    assert (offer.coupon_kind, offer.coupon_pct, offer.coupon_until) == (
        "activation", Decimal("30.00"), None)
    assert (offer.price_from, offer.price_to) == (Decimal("100.00"), Decimal("70.00"))
    item = enqueue_for_rule(rule, NOW)
    expected = "\n".join([
        "🛍️ *Air Fryer Oven Digital 12L*", "",
        "🎟️ CUPOM: ative na página", "",
        "De ❌ ~R$ 100~", "Por 🔥 *R$ 70* (30% OFF)", "⭐ 4,6", "",
        "Cupom: 30% OFF", "",
        "🔗 https://www.amazon.com.br/dp/B0CH5PRZH9?tag=luizahfn-20",
    ])
    assert item.body == expected
    # na coleta seguinte o cupom some: a oferta volta a ser comum (nada de cupom velho)
    plain = Http(page(card(price="100,00", struck="130,00")))
    amazon.collect(NOW + timedelta(hours=2), http=plain, sleep=lambda s: None)
    offer.refresh_from_db()
    assert offer.coupon_kind == "" and offer.coupon_pct is None
