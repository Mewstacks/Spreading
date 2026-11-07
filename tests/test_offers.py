from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apps.compose.offer import build_offer_message, short_name
from apps.delivery.models import OfferDelivery, OutboxItem
from apps.delivery.outbox import deliver_due, enqueue_for_rule, enqueue_offer, explain_rule
from apps.offers import ml_api, niches, selection
from apps.offers.links import safe_affiliate_url
from apps.offers.models import AffiliateLink, Offer, Rule
from apps.offers.selection import in_window, is_live_checkable
from tests.conftest import NOW, FakeTransport

TODAY = date(2026, 11, 7)
IMG = "https://http2.mlstatic.com/D_NQ_NP_1.webp"


def make_offer(key="MLB111", title="Aspirador Robô Xiaomi S40", price=100, frm=200,
               store="mercadolivre", source="v1-catalogo", seen=NOW, image=IMG, url=None, **extra):
    pct = round((1 - price / frm) * 100, 2) if frm else 0
    defaults = dict(
        store=store, source=source, product_key=key, link_key=extra.pop("link_key", key),
        title=title, url=url or f"https://www.mercadolivre.com.br/p/{key}", image_url=image,
        price_from=Decimal(str(frm)), price_to=Decimal(str(price)), discount_pct=Decimal(str(pct)),
        valid_on=seen.date(), last_seen=seen, macro=niches.classify(title),
    )
    defaults.update(extra)
    return Offer.objects.create(**defaults)


@pytest.fixture
def rule(org, destination):
    return Rule.unscoped.create(org=org, destination=destination, name="Grupo",
                                window_start=0, window_end=0, niches=[])


def link(org, key="MLB111", url="https://meli.la/abc"):
    return AffiliateLink.unscoped.create(org=org, product_key=key, affiliate_url=url)


@pytest.fixture(autouse=True)
def live_price(monkeypatch):
    """Preço ao vivo padrão = preço da oferta (100)."""
    state = {"price": 100.0}
    monkeypatch.setattr(
        ml_api, "product_price",
        lambda pid, http=None: {"price": state["price"]} if state["price"] else {},
    )
    monkeypatch.setattr("apps.delivery.outbox.fetch_image_jpeg", lambda url: b"jpeg")
    return state


# ── nichos ────────────────────────────────────────────────────────────────
def test_niche_taxonomy_has_17_macros_and_subniches():
    cat = niches.catalog()
    assert len(cat) == 17
    assert niches.macro_key("Eletrodomésticos") in niches.valid_keys()
    labels = [label for _m, _k, subs in cat for label, _ in subs]
    assert "Robô aspirador" in labels


def test_classify_and_match_semantics():
    title = "Robô Aspirador Xiaomi Vacuum S40"
    macro = "Eletrodomésticos"
    assert niches.classify(title) == macro
    sub = niches.sub_key(macro, "Robô aspirador")
    assert niches.match([sub], title, macro) == "sub"
    assert niches.match([niches.macro_key(macro)], title, macro) == "macro"
    assert niches.match([niches.macro_key("Automotivo")], title, macro) == ""
    assert niches.match([], title, "") == "macro"  # sem nicho escolhido = tudo
    assert niches.match([sub], "Cafeteira Nespresso", macro) == ""


# ── mensagem (goldens do v1) ──────────────────────────────────────────────
def test_short_name_cuts_by_segment_without_dangling_words():
    assert short_name("Monitor LG 24MS500-B 24 polegadas") == "Monitor LG 24MS500-B 24 polegadas"
    long = "Monitor LG 24MS500-B 24 Polegadas Full HD IPS 75Hz, HDMI, Preto - Garantia 12 meses"
    out = short_name(long)
    assert len(out) <= 50 and not out.endswith((" de", " com", " e", ","))
    assert len(short_name("palavra " * 30)) <= 50


def test_golden_no_coupon():
    offer = Offer(title="Monitor LG 24MS500-B 24 polegadas", price_from=Decimal(145),
                  price_to=Decimal("72.50"))
    assert build_offer_message(offer, "https://meli.la/x") == (
        "🛍️ *Monitor LG 24MS500-B 24 polegadas*\n\nDe ❌ ~R$ 145~\nPor 🔥 *R$ 72,50*\n\n"
        "🔗 https://meli.la/x"
    )


def test_golden_code_coupon():
    until = datetime(2026, 11, 30, 23, 59, tzinfo=timezone(timedelta(hours=-3)))
    offer = Offer(title="Monitor LG 24MS500-B 24 polegadas", price_from=Decimal("199.98"),
                  price_to=Decimal("58.20"), coupon_kind="code", coupon_code="APROVEITAMELI",
                  coupon_pct=Decimal(30), coupon_cap=Decimal(500), coupon_min=Decimal(1),
                  coupon_until=until)
    assert build_offer_message(offer, "https://meli.la/2Ehvccw") == (
        "🛍️ *Monitor LG 24MS500-B 24 polegadas*\n\n🎟️ CUPOM: APROVEITAMELI\n\n"
        "De ❌ ~R$ 199,98~\nPor 🔥 *R$ 58,20*\n\n"
        "Cupom: 30% OFF · até R$ 500 · aplique no carrinho\n⏳ Válido até 30/11 às 23h59\n\n"
        "🔗 https://meli.la/2Ehvccw"
    )


def test_activation_coupon_says_activate_and_shows_min_only_above_5():
    offer = Offer(title="X", price_from=Decimal(100), price_to=Decimal(80),
                  coupon_kind="activation", coupon_amount=Decimal(20), coupon_min=Decimal(50))
    text = build_offer_message(offer, "https://meli.la/y")
    assert "🎟️ CUPOM: ative no link" in text and "R$ 20 OFF · compra mín. R$ 50" in text
    assert "aplique no carrinho" not in text
    offer.coupon_min = Decimal(1)
    assert "compra mín." not in build_offer_message(offer, "https://meli.la/y")


# ── seleção ───────────────────────────────────────────────────────────────
def test_window_weekdays_and_midnight(rule):
    assert in_window(rule, NOW)
    rule.window_start, rule.window_end = 20, 6
    assert not in_window(rule, NOW)
    rule.window_start, rule.window_end, rule.weekdays = 8, 20, "1,2"
    assert not in_window(rule, NOW)  # sábado


def test_publishes_best_offer_with_photo_and_owner_link(org, destination, rule):
    make_offer()
    link(org)
    item = enqueue_for_rule(rule, NOW)
    assert item and item.kind == "offer" and item.image_url == IMG
    assert item.body.startswith("🛍️ *Aspirador Robô Xiaomi S40*")
    assert "https://meli.la/abc" in item.body
    transport = FakeTransport()
    assert deliver_due(transport, NOW)["confirmado"] == 1
    assert transport.images and transport.sent == []


def test_no_photo_no_post(org, rule, monkeypatch):
    make_offer()
    link(org)
    monkeypatch.setattr("apps.delivery.outbox.fetch_image_jpeg", lambda url: None)
    report = Counter()
    assert enqueue_offer(org, rule, NOW, report=report) is None
    assert report["sem_foto"] == 1
    Offer.objects.update(image_url="")
    assert dict(explain_rule(rule, NOW)) == {}  # sem imagem nem entra no pool


def test_requires_owner_link_amazon_uses_org_tag(org, rule):
    make_offer(key="B012345678", store="amazon", title="Cafeteira",
               url="https://www.amazon.com.br/dp/B012345678")
    assert explain_rule(rule, NOW)["sem_link_do_dono"] == 1
    org.amazon_tag = "luizahfn-20"
    org.save()
    item = enqueue_for_rule(rule, NOW)
    assert item and "https://www.amazon.com.br/dp/B012345678?tag=luizahfn-20" in item.body


def test_live_original_price_beats_the_observed_one(org, rule, monkeypatch):
    make_offer(price=100, frm=200)
    link(org)
    monkeypatch.setattr(ml_api, "product_price",
                        lambda pid, http=None: {"price": 100.0, "price_from": 250.0})
    item = enqueue_for_rule(rule, NOW)
    assert "~R$ 250~" in item.body and "~R$ 200~" not in item.body  # o "De" é o do anúncio agora
    OutboxItem.unscoped.all().delete()
    OfferDelivery.unscoped.all().delete()
    monkeypatch.setattr(ml_api, "product_price",
                        lambda pid, http=None: {"price": 100.0, "price_from": 105.0})
    assert enqueue_for_rule(rule, NOW) is None  # o anúncio hoje quase não tem desconto
    assert explain_rule(rule, NOW)["prontas"] == 1  # passou na seleção, caiu na prova ao vivo


def test_candidates_lists_offers_that_only_lack_the_owner_link(org, destination, rule):
    make_offer(key="MLB111", price=100, frm=200)  # sem link
    make_offer(key="MLB222", price=150, frm=200)  # com link
    make_offer(key="MLB333", price=50, frm=100, link_key="MLB333+CODE", coupon_kind="code",
               coupon_code="CODE", coupon_until=NOW + timedelta(days=5))  # par sem link
    make_offer(key="MLB444", price=190, frm=200)  # 5%: abaixo do mínimo, nem é candidata
    link(org, key="MLB222")
    missing = []
    ready = list(selection.candidates(org, destination, rule, NOW, missing=missing))
    assert [o.product_key for o, _link in ready] == ["MLB222"]
    assert [o.link_key for o, _score in missing] == ["MLB333+CODE", "MLB111"]  # par antes
    same = [o.product_key for o, _link in selection.candidates(org, destination, rule, NOW)]
    assert same == ["MLB222"]  # sem `missing` o resultado é o mesmo


def test_safe_affiliate_url_rejects_storefront_and_foreign_hosts():
    assert safe_affiliate_url("https://meli.la/2Ehvccw") == "https://meli.la/2Ehvccw"
    for bad in ["https://www.mercadolivre.com.br/social/lphoffmann?matt_word=x&matt_tool=1",
                "https://www.mercadolivre.com.br/social/loja/lists", "http://meli.la/x",
                "https://evil.io/meli.la", ""]:
        assert safe_affiliate_url(bad) == "", bad


def test_storefront_link_is_never_published_even_if_stored(org, rule):
    make_offer()
    link(org, url="https://www.mercadolivre.com.br/social/loja?matt_word=x&matt_tool=1&ref=abc")
    assert enqueue_for_rule(rule, NOW) is None
    assert explain_rule(rule, NOW)["sem_link_do_dono"] == 1
    assert OutboxItem.unscoped.count() == 0


def test_stores_filter_and_freshness(org, rule):
    make_offer(key="B012345678", store="amazon", seen=NOW - timedelta(hours=13),
               url="https://www.amazon.com.br/dp/B012345678")
    assert explain_rule(rule, NOW)["sem_prova_de_preco"] == 1
    make_offer(key="MLB777", url="https://produto.mercadolivre.com.br/MLB-777777777")
    assert explain_rule(rule, NOW)["sem_prova_de_preco"] == 2  # anúncio solto: a API não mede
    assert not is_live_checkable(Offer(product_key="MLB777", store="mercadolivre", url="x"))
    make_offer(key="MLB888", seen=NOW - timedelta(days=15))
    assert explain_rule(rule, NOW)["sem_prova_de_preco"] == 2  # fora de 14 dias nem entra
    rule.stores = "shopee"
    rule.save()
    assert enqueue_for_rule(rule, NOW) is None


def test_niche_choice_filters_and_ranks(org, rule):
    make_offer(key="MLB1", title="Robô Aspirador Dreame F10", price=100, frm=200)
    make_offer(key="MLB2", title="Colchão Queen Luuna", price=100, frm=300)
    link(org, "MLB1")
    link(org, "MLB2")
    rule.niches = [niches.sub_key("Casa, Móveis e Decoração", "Colchões")]
    rule.save()
    item = enqueue_for_rule(rule, NOW)
    assert item and "Colchão" in item.body
    rule.niches = [niches.macro_key("Automotivo")]
    rule.save()
    OutboxItem.unscoped.all().delete()
    OfferDelivery.unscoped.all().delete()
    later = NOW + timedelta(hours=3)
    assert enqueue_for_rule(rule, later) is None
    assert explain_rule(rule, later)["fora_do_nicho"] == 2


def test_min_discount_price_range_and_cooldown(org, destination, rule):
    make_offer(key="MLB1", price=190, frm=200)  # 5%
    link(org, "MLB1")
    assert enqueue_for_rule(rule, NOW) is None
    make_offer(key="MLB2", price=100, frm=200)
    link(org, "MLB2")
    rule.price_max = Decimal(50)
    rule.save()
    assert enqueue_for_rule(rule, NOW) is None
    rule.price_max = None
    rule.save()
    assert enqueue_for_rule(rule, NOW)
    deliver_due(FakeTransport(), NOW)
    assert enqueue_for_rule(rule, NOW + timedelta(hours=2)) is None  # mesmo produto em cooldown


def test_coupon_pairs_go_first_and_only_with_matching_live_base_price(org, rule, live_price):
    make_offer(key="MLB1", title="Produto Comum", price=100, frm=300)
    make_offer(
        key="MLB2", title="Produto Com Cupom", price=120, frm=200, link_key="MLB2+VALE10",
        coupon_kind="code", coupon_code="VALE10", coupon_pct=Decimal(10),
        proof_base_price=Decimal("130.00"), coupon_until=NOW + timedelta(days=1), proof_at=NOW,
    )
    link(org, "MLB1")
    link(org, "MLB2+VALE10")
    live_price["price"] = 100.0  # base ao vivo ≠ preço da prova (130): par inválido
    item = enqueue_for_rule(rule, NOW)
    assert item and "Produto Comum" in item.body
    OutboxItem.unscoped.all().delete()
    OfferDelivery.unscoped.all().delete()
    live_price["price"] = 130.0
    item = enqueue_for_rule(rule, NOW + timedelta(hours=3))
    assert item and "CUPOM: VALE10" in item.body and "Por 🔥 *R$ 120*" in item.body


def test_live_price_drop_to_no_discount_is_not_published(org, rule, live_price):
    make_offer(price=100, frm=200)
    link(org)
    live_price["price"] = 199.0  # desconto caiu para <15%
    report = Counter()
    assert enqueue_offer(org, rule, NOW, report=report) is None
    assert report["desconto_caiu"] == 1
    live_price["price"] = 0
    report = Counter()
    assert enqueue_offer(org, rule, NOW, report=report) is None
    assert report["preco_inconclusivo"] == 1


def test_live_price_is_what_gets_announced(org, rule, live_price):
    make_offer(price=100, frm=200)
    link(org)
    live_price["price"] = 90.0
    item = enqueue_for_rule(rule, NOW)
    assert "Por 🔥 *R$ 90*" in item.body and "De ❌ ~R$ 200~" in item.body


def test_rule_interval_and_daily_cap(org, destination, rule):
    rule.daily_cap = 1
    rule.save()
    make_offer(key="MLB1")
    make_offer(key="MLB2")
    link(org, "MLB1")
    link(org, "MLB2")
    assert enqueue_for_rule(rule, NOW)
    deliver_due(FakeTransport(), NOW)
    assert enqueue_for_rule(rule, NOW + timedelta(minutes=10)) is None
    assert enqueue_for_rule(rule, NOW + timedelta(hours=2)) is None  # teto diário


def test_rule_daily_cap_ceiling():
    assert Rule(daily_cap=99).effective_cap == 30


def test_photo_failure_at_send_keeps_cooldown_no_loop(org, destination, rule, monkeypatch):
    make_offer()
    link(org)
    item = enqueue_for_rule(rule, NOW)
    monkeypatch.setattr("apps.delivery.outbox.fetch_image_jpeg", lambda url: None)
    deliver_due(FakeTransport(), NOW)
    item.refresh_from_db()
    assert item.state == OutboxItem.FAILED and item.last_error == "foto_indisponivel"
    assert OfferDelivery.objects.for_org(org).count() == 1  # não volta ao pool na hora
