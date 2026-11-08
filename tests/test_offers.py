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
        "🛍️ *Monitor LG 24MS500-B 24 polegadas*\n\nDe ❌ ~R$ 145~\nPor 🔥 *R$ 72,50* (50% OFF)\n\n"
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
        "De ❌ ~R$ 199,98~\nPor 🔥 *R$ 58,20* (71% OFF)\n\n"
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


def code_pair(key="MLB555", code="VALE", **extra):
    return make_offer(
        key=key, price=80, frm=100, link_key=f"{key}+{code}", coupon_kind="code",
        coupon_code=code, coupon_pct=Decimal(20), proof_base_price=Decimal("100.00"),
        coupon_until=NOW + timedelta(days=3), proof_at=NOW, **extra)


def test_a_code_coupon_pair_rides_on_the_product_link(org, destination, rule):
    code_pair()
    link(org, "MLB555", "https://meli.la/produto")  # só o do produto: o código vale no carrinho
    missing = []
    ready = list(selection.candidates(org, destination, rule, NOW, missing=missing))
    assert [(o.link_key, url) for o, url in ready] == [("MLB555+VALE", "https://meli.la/produto")]
    assert missing == []  # o Link Builder não gasta tentativa em quem já tem link
    item = enqueue_for_rule(rule, NOW)
    assert "CUPOM: VALE" in item.body and "https://meli.la/produto" in item.body


def test_the_pair_link_beats_the_product_link(org, destination, rule):
    code_pair()
    link(org, "MLB555", "https://meli.la/produto")
    link(org, "MLB555+VALE", "https://meli.la/par")
    assert [url for _o, url in selection.candidates(org, destination, rule, NOW)] == [
        "https://meli.la/par"]


def test_an_activation_pair_still_needs_a_link_that_carries_the_campaign(org, destination, rule):
    make_offer(key="MLB777", price=80, frm=100, link_key="MLB777+c999", coupon_kind="activation",
               coupon_amount=Decimal(20), proof_base_price=Decimal("100.00"),
               coupon_until=NOW + timedelta(days=3), proof_at=NOW)
    link(org, "MLB777", "https://meli.la/produto")
    missing = []
    assert list(selection.candidates(org, destination, rule, NOW, missing=missing)) == []
    assert [o.link_key for o, _score in missing] == ["MLB777+c999"]


def test_the_product_link_never_serves_a_storefront_or_another_store(org):
    pair = code_pair()
    links = {"MLB555": "https://www.mercadolivre.com.br/social/loja?matt_word=x&matt_tool=1"}
    assert selection.resolve_link(org, pair, links) == ""
    amazon = make_offer(key="B0ABCDEFGH", store="amazon", link_key="B0ABCDEFGH+X",
                        coupon_kind="code", coupon_code="X")
    assert selection.resolve_link(org, amazon, {"B0ABCDEFGH": "https://meli.la/x"}) == ""


def test_enqueue_specific_also_uses_the_product_link_for_a_code_pair(org, destination, rule):
    from apps.delivery.outbox import enqueue_specific

    pair = code_pair()
    assert enqueue_specific(org, rule, pair, NOW) == (None, "sem_link_do_dono")
    link(org, "MLB555", "https://meli.la/produto")
    item, reason = enqueue_specific(org, rule, pair, NOW)
    assert reason == "" and "https://meli.la/produto" in item.body


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


def test_coupon_mode_only_first_and_off(org, destination, rule):
    make_offer(key="MLB111", price=100, frm=200)  # 50%, sem cupom
    make_offer(key="MLB333", price=60, frm=100, link_key="MLB333+CODE", coupon_kind="code",
               coupon_code="CODE", coupon_until=NOW + timedelta(days=5))  # 40%, par
    link(org, key="MLB111")
    link(org, key="MLB333+CODE")

    def order():
        return [o.link_key for o, _l in selection.candidates(org, destination, rule, NOW)]

    assert order() == ["MLB333+CODE", "MLB111"]  # padrão: par cupom primeiro
    rule.coupon_mode = "only"
    assert order() == ["MLB333+CODE"]
    rule.coupon_mode = "off"
    assert order() == ["MLB111"]


def test_enqueue_specific_sends_the_chosen_offer_with_the_same_proofs(org, destination, rule):
    from apps.delivery.outbox import enqueue_specific

    offer = make_offer(key="MLB111", price=100, frm=200)
    assert enqueue_specific(org, rule, offer, NOW) == (None, "sem_link_do_dono")
    link(org, key="MLB111")
    item, reason = enqueue_specific(org, rule, offer, NOW)
    assert reason == "" and item.kind == "offer" and "https://meli.la/abc" in item.body
    assert enqueue_specific(org, rule, offer, NOW)[1] == "grupo_indisponivel"  # há item na fila


def test_enqueue_specific_refuses_stale_expired_and_unproven(org, destination, rule, live_price):
    from apps.delivery.outbox import enqueue_specific

    link(org, key="MLB111")
    link(org, key="MLB222+CODE")
    stale = make_offer(key="MLB111", seen=NOW - timedelta(days=20))
    assert enqueue_specific(org, rule, stale, NOW)[1] == "observacao_velha"
    expired = make_offer(key="MLB222", link_key="MLB222+CODE", coupon_kind="code",
                         coupon_code="C", coupon_until=NOW - timedelta(hours=1))
    assert enqueue_specific(org, rule, expired, NOW)[1] == "cupom_vencido"
    live_price["price"] = 0
    fresh = make_offer(key="MLB111", price=100, frm=200)
    assert enqueue_specific(org, rule, fresh, NOW)[1] == "preco_inconclusivo"


def test_huge_discount_needs_the_listing_to_confirm_the_original_price(org, rule, monkeypatch):
    make_offer(price=100, frm=600)  # 83% só na observação antiga
    link(org)
    monkeypatch.setattr(ml_api, "product_price", lambda pid, http=None: {"price": 100.0})
    assert enqueue_for_rule(rule, NOW) is None
    assert explain_rule(rule, NOW)["prontas"] == 1
    monkeypatch.setattr(ml_api, "product_price",
                        lambda pid, http=None: {"price": 100.0, "price_from": 600.0})
    assert enqueue_for_rule(rule, NOW) is not None  # o anúncio mostra o "De" agora


# ── guarda de queda real ──────────────────────────────────────────────────
def test_a_steady_price_is_not_a_promotion_but_a_real_drop_is(org, destination, rule):
    link(org)
    for ago in (1, 2, 3, 4):  # quatro dias a 100: o "De 200" é de fachada
        make_offer(price=100, frm=200, seen=NOW - timedelta(days=ago))
    make_offer(price=100, frm=200)
    report = Counter()
    assert list(selection.candidates(org, destination, rule, NOW, report)) == []
    assert report["sem_queda_real"] == 5
    Offer.objects.filter(valid_on=NOW.date()).update(price_to=Decimal("80"))  # queda de verdade
    report = Counter()
    [(offer, _link)] = list(selection.candidates(org, destination, rule, NOW, report))
    assert offer.price_to == Decimal("80") and report["sem_queda_real"] == 4


def test_the_history_guard_waits_for_enough_observations_and_skips_coupon_pairs(
        org, destination, rule):
    link(org)
    for ago in (1, 2):  # 3 observações no total: ainda não dá para dizer
        make_offer(price=100, frm=200, seen=NOW - timedelta(days=ago))
    make_offer(price=100, frm=200)
    report = Counter()
    assert list(selection.candidates(org, destination, rule, NOW, report))
    assert report["sem_queda_real"] == 0
    link(org, key="MLB333+ALFA")
    for ago in range(5):  # par com cupom a 60 há dias: o abatimento é o cupom
        make_offer(key="MLB333", link_key="MLB333+ALFA", price=60, frm=100, coupon_kind="code",
                   coupon_code="ALFA", coupon_pct=Decimal(30), seen=NOW - timedelta(days=ago),
                   coupon_until=NOW + timedelta(days=5))
    report = Counter()
    first = next(iter(selection.candidates(org, destination, rule, NOW, report)))
    assert first[0].coupon_code == "ALFA" and report["sem_queda_real"] == 0


def test_the_link_builder_queue_skips_fake_promotions_too(org, destination, rule):
    for ago in range(5):
        make_offer(key="MLB777", price=100, frm=200, seen=NOW - timedelta(days=ago))
    missing = []
    assert list(selection.candidates(org, destination, rule, NOW, missing=missing)) == []
    assert missing == []


def test_commission_breaks_ties_in_the_score(db):
    plain = make_offer(key="MLB1")
    low = make_offer(key="MLB2", commission_pct=Decimal(2))
    high = make_offer(key="MLB3", commission_pct=Decimal(18))
    low_score, plain_score, high_score = (
        selection.score(o, "sub", NOW) for o in (low, plain, high))
    assert low_score < plain_score < high_score


def test_the_rule_interval_varies_by_send_but_stays_within_a_quarter(org, destination, rule):
    from apps.delivery.outbox import jittered_interval

    waits = set()
    for n in range(40):
        item = OutboxItem.unscoped.create(
            org=org, destination=destination, rule=rule, body=str(n), created_at=NOW,
            state=OutboxItem.CONFIRMED, idempotency_key=f"k{n}", next_attempt_at=NOW)
        wait = jittered_interval(rule, item)
        assert timedelta(minutes=45) <= wait <= timedelta(minutes=75)  # 60 min ± 25%
        assert jittered_interval(rule, item) == wait  # o mesmo envio espera sempre o mesmo
        waits.add(wait)
    assert len(waits) > 20  # e envios diferentes esperam tempos diferentes


# ── dedupe por nome: o mesmo produto em anúncios diferentes não repete no grupo ──────────────
BASE = "Fritadeira Elétrica Sem Óleo Air Fryer Mondial 5L"  # 49 letras: o nome curto da mensagem


def shopee_listing(item, title, price=300, frm=800):
    key = f"shp:{item}"
    offer = make_offer(key=key, title=title, price=price, frm=frm, store="shopee",
                       source="shopee-api", url=f"https://shopee.com.br/product/1/{item}")
    return offer


def deliver_and_clear(now):
    deliver_due(FakeTransport(), now)


@pytest.fixture
def quick(rule):
    """Regra sem espera entre envios: o que barra é só o cooldown do produto."""
    Rule.unscoped.filter(id=rule.id).update(interval_minutes=1, daily_cap=30)
    rule.refresh_from_db()
    return rule


def test_title_identity_ignores_case_accents_punctuation_and_the_cut():
    from apps.offers.keys import title_identity

    assert title_identity("Fritadeira Elétrica, Air-Fryer 5L!") == title_identity(
        "FRITADEIRA ELETRICA AIR FRYER 5L")
    assert title_identity(BASE + " Grand Family Preta 127V") == title_identity(
        BASE + " Grand Family Branca 220V") == title_identity(BASE)
    assert title_identity("Aspirador Robô Xiaomi S40") != title_identity(
        "Aspirador Robô Xiaomi S10")
    assert title_identity("") == "" and title_identity("***") == ""


def test_listings_with_the_same_shown_name_are_not_sent_twice_in_the_cooldown(
        org, destination, quick):
    rule = quick
    for item in (1, 2, 3):
        link(org, f"shp:{item}", f"https://s.shopee.com.br/{item}")
    shopee_listing(1, BASE + " Grand Family Preta 127V")
    shopee_listing(2, BASE + " Grand Family Branca 220V")  # outro anúncio, mesmo nome na mensagem
    shopee_listing(3, "Aspirador Robô Xiaomi S40 Branco", price=500, frm=900)
    first = enqueue_for_rule(rule, NOW)
    assert first
    names = {first.body.split("*")[1]}
    deliver_and_clear(NOW)
    second = enqueue_for_rule(rule, NOW + timedelta(hours=1))
    names.add(second.body.split("*")[1])
    deliver_and_clear(NOW + timedelta(hours=1))
    assert names == {BASE, "Aspirador Robô Xiaomi S40 Branco"}  # um de cada, nunca a fritadeira 2x
    assert enqueue_for_rule(rule, NOW + timedelta(hours=2)) is None
    # a fritadeira enviada, a outra com o mesmo nome e o aspirador enviado: três em cooldown
    assert explain_rule(rule, NOW + timedelta(hours=2))["cooldown"] == 3


def test_the_same_name_is_allowed_again_after_the_cooldown(org, destination, rule):
    link(org, "shp:1", "https://s.shopee.com.br/1")
    link(org, "shp:2", "https://s.shopee.com.br/2")
    shopee_listing(1, BASE + " Preta")
    assert enqueue_for_rule(rule, NOW)
    deliver_and_clear(NOW)
    later = NOW + timedelta(hours=rule.cooldown_hours + 1)
    other = shopee_listing(2, BASE + " Branca", price=250)
    Offer.objects.filter(id=other.id).update(last_seen=later, valid_on=later.date())
    Offer.objects.filter(product_key="shp:1").update(last_seen=later - timedelta(days=30))
    assert enqueue_for_rule(rule, later)


def test_the_delivery_records_the_identity_and_old_rows_still_block_by_key(org, destination, quick):
    rule = quick
    from apps.offers.keys import title_identity

    link(org, "shp:1", "https://s.shopee.com.br/1")
    link(org, "shp:2", "https://s.shopee.com.br/2")
    shopee_listing(1, BASE + " Preta")
    item = enqueue_for_rule(rule, NOW)
    assert OfferDelivery.unscoped.get(outbox_item=item).identity == title_identity(BASE)
    deliver_and_clear(NOW)
    OfferDelivery.unscoped.update(identity="")  # linha antiga, de antes do campo
    shopee_listing(2, "Outro Produto Qualquer Aqui", price=100, frm=400)
    assert "Outro Produto" in enqueue_for_rule(rule, NOW + timedelta(hours=1)).body
    deliver_and_clear(NOW + timedelta(hours=1))
    # a mesma chave de produto ainda bloqueia, como antes, mesmo sem a identidade gravada
    assert enqueue_for_rule(rule, NOW + timedelta(hours=2)) is None


def test_the_manual_send_skips_the_cooldown_but_keeps_the_record(org, destination, rule):
    from apps.delivery.outbox import enqueue_specific
    from apps.offers.keys import title_identity

    link(org, "shp:1", "https://s.shopee.com.br/1")
    link(org, "shp:2", "https://s.shopee.com.br/2")
    shopee_listing(1, BASE + " Preta")
    second = shopee_listing(2, BASE + " Branca", price=250)
    enqueue_for_rule(rule, NOW)
    deliver_and_clear(NOW)
    item, reason = enqueue_specific(org, rule, second, NOW + timedelta(hours=1))
    assert reason == "" and OfferDelivery.unscoped.get(outbox_item=item).identity == (
        title_identity(BASE))


def test_the_migration_recovers_the_identity_from_the_message_the_group_saw():
    import importlib

    from apps.compose.offer import build_offer_message
    from apps.offers.keys import title_identity

    migration = importlib.import_module("apps.delivery.migrations.0003_message_facts")
    for title in (BASE + " Grand Family Preta", "Aspirador Robô Xiaomi S40", "Câmera Wi-Fi 360°"):
        body = build_offer_message(
            Offer(title=title, price_from=Decimal(100), price_to=Decimal(50)), "https://x.io/l")
        assert migration.identity_from_body(body) == title_identity(title)
    assert migration.identity_from_body("sem negrito") == ""
    assert migration.identity_from_body("") == ""
