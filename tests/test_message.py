from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.compose.offer import build_offer_message

LINK = "https://meli.la/x"


def offer(**over):
    base = dict(
        title="Air Fryer Oven 12L WAP", store="mercadolivre", price_from=Decimal("849.86"),
        price_to=Decimal("499"), coupon_kind="", coupon_code="", coupon_pct=None,
        coupon_amount=None, coupon_cap=None, coupon_min=None, coupon_until=None,
        free_shipping=None, official_store=None, rating=None, sales=None, seller="")
    base.update(over)
    return SimpleNamespace(**base)


def lines(text):
    return text.split("\n")


def test_the_discount_is_stated_next_to_the_price_when_there_is_a_de():
    assert build_offer_message(offer(), LINK) == (
        "🛍️ *Air Fryer Oven 12L WAP*\n\nDe ❌ ~R$ 849,86~\nPor 🔥 *R$ 499* (41% OFF)\n\n"
        "🔗 https://meli.la/x")


@pytest.mark.parametrize("change", [
    dict(price_from=None), dict(price_from=Decimal("499")), dict(price_from=Decimal("400")),
    dict(price_from=Decimal("500"))])  # sem "De", "De" igual ou menor, ou desconto abaixo de 1%
def test_no_discount_badge_without_a_real_drop(change):
    text = build_offer_message(offer(**change), LINK)
    assert "OFF" not in text
    assert ("De ❌" in text) is (change["price_from"] == Decimal("500"))


def test_ml_says_free_shipping_and_official_store_when_the_listing_does():
    text = build_offer_message(offer(free_shipping=True, official_store=True), LINK)
    assert lines(text)[3:5] == ["Por 🔥 *R$ 499* (41% OFF)", "🚚 Frete grátis · 🏪 Loja oficial"]
    assert "🚚 Frete grátis" in build_offer_message(offer(free_shipping=True), LINK)
    assert "🏪 Loja oficial" in build_offer_message(offer(official_store=True), LINK)
    plain = build_offer_message(offer(free_shipping=False, official_store=False), LINK)
    assert "🚚" not in plain and "🏪" not in plain  # falso e desconhecido não viram linha


def test_shopee_shows_rating_sales_and_the_shop_on_their_own_lines():
    text = build_offer_message(offer(
        store="shopee", price_from=Decimal("803.95"), price_to=Decimal("297.46"),
        rating=Decimal("4.9"), sales=584, seller="TECNOLAR OFICIAL"), "https://s.shopee.com.br/x")
    assert lines(text)[2:7] == [
        "De ❌ ~R$ 803,95~", "Por 🔥 *R$ 297,46* (63% OFF)", "⭐ 4,9 · 584 vendidos",
        "🏪 TECNOLAR OFICIAL", ""]


@pytest.mark.parametrize("sales, shown", [
    (1, "1 vendido"), (999, "999 vendidos"), (1000, "1 mil vendidos"), (1234, "1,2 mil vendidos"),
    (12000, "12 mil vendidos"), (12499, "12 mil vendidos"), (0, ""), (None, "")])
def test_sales_are_written_the_way_the_shopee_listing_writes_them(sales, shown):
    text = build_offer_message(offer(rating=Decimal("4.7"), sales=sales), LINK)
    assert ("⭐ 4,7 · " + shown if shown else "⭐ 4,7") in lines(text)


def test_amazon_shows_only_the_rating_it_has():
    text = build_offer_message(offer(store="amazon", rating=Decimal("4.6")), LINK)
    assert "⭐ 4,6" in lines(text) and "vendidos" not in text and "🏪" not in text


@pytest.mark.parametrize("rating", [None, Decimal("0"), Decimal("5.1"), Decimal("-1")])
def test_an_implausible_rating_is_not_printed(rating):
    assert "⭐" not in build_offer_message(offer(rating=rating, sales=500), LINK)


def test_the_shop_name_cannot_break_the_whatsapp_formatting():
    nasty = "*Loja* ~boa~\n_oficial_ `x`" + "y" * 80
    text = build_offer_message(offer(seller=nasty), LINK)
    [shop] = [line for line in lines(text) if line.startswith("🏪")]
    assert not set("*_~`") & set(shop) and len(shop) <= 2 + 40  # emoji, espaço e até 40 letras
    assert "Loja boa oficial x" in shop
    assert "🏪" not in build_offer_message(offer(seller="  *_~  "), LINK)


def test_the_extra_lines_sit_under_the_price_and_above_the_coupon_details():
    until = datetime(2026, 11, 30, 23, 59, tzinfo=timezone(timedelta(hours=-3)))
    text = build_offer_message(offer(
        coupon_kind="code", coupon_code="VALE", coupon_pct=Decimal(20), price_to=Decimal("420"),
        price_from=Decimal("800"), coupon_until=until, free_shipping=True,
        official_store=True), LINK)
    assert text == (
        "🛍️ *Air Fryer Oven 12L WAP*\n\n🎟️ CUPOM: VALE\n\n"
        "De ❌ ~R$ 800~\nPor 🔥 *R$ 420* (48% OFF)\n🚚 Frete grátis · 🏪 Loja oficial\n\n"
        "Cupom: 20% OFF · aplique no carrinho\n⏳ Válido até 30/11 às 23h59\n\n"
        "🔗 https://meli.la/x")


def test_an_offer_object_without_the_new_fields_still_renders():
    old = SimpleNamespace(title="X", price_from=Decimal(100), price_to=Decimal(80),
                          coupon_kind="")
    assert build_offer_message(old, LINK) == (
        "🛍️ *X*\n\nDe ❌ ~R$ 100~\nPor 🔥 *R$ 80* (20% OFF)\n\n🔗 https://meli.la/x")
