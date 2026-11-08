from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.offers import amazon, manual, ml_api, selection, shopee
from apps.offers.manual import Coupon
from apps.offers.models import AffiliateLink, Offer
from tests.conftest import NOW
from tests.test_amazon import card as amazon_card
from tests.test_amazon import page as amazon_page

pytestmark = pytest.mark.django_db

KEY = "MLB42225245"
ML_URL = f"https://www.mercadolivre.com.br/air-fryer/p/{KEY}?pdp_filters=x#poly_card"
IMG = "https://http2.mlstatic.com/D_NQ_NP_898071-MLA99545431524_122025-F.jpg"
UNTIL = (NOW + timedelta(days=5)).date()


@pytest.fixture
def ml(monkeypatch):
    """API do ML: produto ativo, R$ 500 por R$ 800 (padrão); cada teste muda o dict."""
    state = {"info": {"name": "Air Fryer Oven 12L WAP", "image": IMG, "status": "active"},
             "price": {"price": 500.0, "price_from": 800.0, "winner_tagged": True}}
    monkeypatch.setattr(ml_api, "product", lambda pid, http=None: state["info"])
    monkeypatch.setattr(ml_api, "product_price", lambda pid, http=None: state["price"])
    return state


def add(org, text, coupon=None, **kw):
    return manual.add(org, text, coupon=coupon, now=NOW, **kw)


def code(**over):
    base = dict(code="VALE20", kind="pct", value=Decimal(20), minimum=None, cap=None, until=UNTIL)
    base.update(over)
    return Coupon(**base)


# ── leitura do que foi colado ─────────────────────────────────────────────
def test_parse_takes_one_product_per_line_with_an_optional_owner_link():
    text = f"""
      {ML_URL}  https://meli.la/abc
      https://www.amazon.com.br/dp/B0CH5PRZH9
      não é link
    """
    assert manual.parse(text) == [
        (ML_URL, "https://meli.la/abc"), ("https://www.amazon.com.br/dp/B0CH5PRZH9", ""),
        ("não é link", "")]


def test_parse_keeps_only_the_first_twenty_lines():
    lines = "\n".join(f"https://www.amazon.com.br/dp/B0CH5PRZ{n:02d}" for n in range(25))
    assert len(manual.parse(lines)) == manual.MAX_LINES


@pytest.mark.parametrize("url, expected", [
    (ML_URL, ("mercadolivre", KEY)),
    ("https://produto.mercadolivre.com.br/p/MLB1", ("mercadolivre", "MLB1")),
    ("https://www.amazon.com.br/Nome/dp/b0ch5prz9a/ref=x", ("amazon", "B0CH5PRZ9A")),
    ("https://www.amazon.com.br/gp/product/B0CH5PRZ9A", ("amazon", "B0CH5PRZ9A")),
    ("https://shopee.com.br/product/1006215031/40856700906", ("shopee", "40856700906")),
    ("https://shopee.com.br/Suporte-de-mesa-i.1006215031.40856700906?sp=1",
     ("shopee", "40856700906")),
])
def test_identify_reads_the_product_of_each_store(url, expected):
    assert manual.identify(url) == (*expected, "")


@pytest.mark.parametrize("url, reason", [
    ("https://produto.mercadolivre.com.br/MLB-123456789-air-fryer-_JM", "anuncio_solto"),
    ("https://meli.la/2Ehvccw", "link_curto"),
    ("https://s.shopee.com.br/AbCd123", "link_curto"),
    ("https://amzn.to/3xyz", "link_curto"),
    ("https://www.amazon.com.br/s?k=air+fryer", "link_invalido"),
    ("http://www.mercadolivre.com.br/p/MLB1", "link_invalido"),
    ("https://evil.example.com/mercadolivre.com.br/p/MLB1", "loja_desconhecida"),
    ("https://mercadolivre.com.br.evil.io/p/MLB1", "loja_desconhecida"),
    ("não é link", "link_invalido"),
])
def test_identify_refuses_what_it_cannot_prove(url, reason):
    assert manual.identify(url) == ("", "", reason)


# ── Mercado Livre ─────────────────────────────────────────────────────────
def test_an_ml_product_becomes_an_offer_with_the_official_prices(org, ml):
    AffiliateLink.unscoped.create(org=org, product_key=KEY, affiliate_url="https://meli.la/abc")
    [out] = add(org, ML_URL).outcomes
    assert (out.status, out.reason, out.store, out.key) == ("ok", "", "mercadolivre", KEY)
    offer = Offer.objects.get(product_key=KEY)
    assert (offer.source, offer.link_key, offer.valid_on) == ("manual", KEY, NOW.date())
    assert (offer.price_to, offer.price_from, offer.discount_pct) == (
        Decimal("500.00"), Decimal("800.00"), Decimal("37.50"))
    assert offer.url == f"https://www.mercadolivre.com.br/p/{KEY}" and offer.image_url == IMG
    assert offer.title == "Air Fryer Oven 12L WAP" and offer.last_seen == NOW
    assert offer.coupon_kind == "" and selection.is_live_checkable(offer)


def test_the_owner_link_pasted_with_the_product_is_stored(org, ml):
    [out] = add(org, f"{ML_URL} https://meli.la/zzz").outcomes
    assert out.status == "ok"
    assert AffiliateLink.objects.for_org(org).get(product_key=KEY).affiliate_url == (
        "https://meli.la/zzz")


def test_without_an_owner_link_the_offer_enters_but_waits_for_the_link(org, ml):
    [out] = add(org, ML_URL).outcomes
    assert (out.status, out.reason) == ("sem_link", "") and out.offer.product_key == KEY
    [bad] = add(org, f"{ML_URL} https://evil.io/meli.la").outcomes  # link que não é do dono
    assert bad.status == "sem_link" and not AffiliateLink.objects.for_org(org).exists()
    [storefront] = add(
        org, f"{ML_URL} https://www.mercadolivre.com.br/social/loja?matt_word=x").outcomes
    assert storefront.status == "sem_link" and not AffiliateLink.objects.for_org(org).exists()


def test_pasting_twice_updates_the_same_offer(org, ml):
    add(org, ML_URL)
    ml["price"] = {"price": 450.0, "price_from": 800.0, "winner_tagged": True}
    add(org, f"{ML_URL}\n{ML_URL}")
    [offer] = Offer.objects.filter(product_key=KEY)
    assert offer.price_to == Decimal("450.00")


@pytest.mark.parametrize("change, reason", [
    (lambda s: s.update(price={}), "preco_inconclusivo"),
    (lambda s: s.update(info={}), "preco_inconclusivo"),
    (lambda s: s["info"].update(status="paused"), "inativo"),
    (lambda s: s["info"].update(image=""), "sem_foto"),
    (lambda s: s["info"].update(image="https://evil.io/x.jpg"), "sem_foto"),
    (lambda s: s["info"].update(name="Carro usado Gol 2010"), "categoria_proibida"),
    (lambda s: s.update(price={"price": 500.0, "price_from": 0.0}), "sem_desconto"),
    (lambda s: s.update(price={"price": 500.0, "price_from": 500.0}), "sem_desconto"),
])
def test_what_the_api_cannot_prove_never_becomes_an_offer(org, ml, change, reason):
    change(ml)
    [out] = add(org, ML_URL).outcomes
    assert (out.status, out.reason) == ("erro", reason)
    assert not Offer.objects.exists()


# ── cupom manual (ML) ─────────────────────────────────────────────────────
def test_a_code_coupon_makes_a_pair_proved_on_the_live_price(org, ml):
    ml["price"] = {"price": 500.0, "price_from": 0.0}  # sem "De": o desconto é o cupom
    [out] = add(org, ML_URL, code(code="vale20", cap=Decimal(80), minimum=Decimal(100))).outcomes
    assert out.status == "sem_link"
    pair = Offer.objects.get(link_key=f"{KEY}+VALE20")
    assert (pair.product_key, pair.coupon_kind, pair.coupon_code) == (KEY, "code", "VALE20")
    assert (pair.coupon_pct, pair.coupon_cap, pair.coupon_min) == (
        Decimal(20), Decimal(80), Decimal(100))
    # 20% de 500 = 100, o teto de 80 manda: 500 - 80
    assert (pair.price_from, pair.price_to, pair.discount_pct) == (
        Decimal("500.00"), Decimal("420.00"), Decimal("16.00"))
    assert (pair.proof_base_price, pair.proof_at) == (Decimal("500.00"), NOW)
    assert pair.coupon_until.isoformat() == "2026-11-13T02:59:59+00:00"  # fim do dia, em Brasília


def test_a_fixed_amount_coupon_and_the_original_price_of_the_listing(org, ml):
    add(org, ML_URL, code(code="DEZ", kind="amount", value=Decimal(50)))
    pair = Offer.objects.get(link_key=f"{KEY}+DEZ")
    assert (pair.coupon_amount, pair.coupon_pct) == (Decimal(50), None)
    assert (pair.price_from, pair.price_to) == (Decimal("800.00"), Decimal("450.00"))


def test_a_coupon_without_a_code_is_an_activation_pair(org, ml):
    add(org, f"{ML_URL} https://meli.la/campanha", code(code=""))
    pair = Offer.objects.get(product_key=KEY)
    assert (pair.coupon_kind, pair.coupon_code, pair.link_key) == (
        "activation", "", f"{KEY}+ativar")
    assert AffiliateLink.objects.for_org(org).get(product_key=f"{KEY}+ativar").affiliate_url == (
        "https://meli.la/campanha")


def test_a_manual_code_pair_rides_on_the_product_link_already_in_stock(org, ml):
    AffiliateLink.unscoped.create(org=org, product_key=KEY, affiliate_url="https://meli.la/abc")
    [out] = add(org, ML_URL, code()).outcomes
    assert out.status == "ok"  # o código vale no carrinho: o link do produto basta
    pair = Offer.objects.get(link_key=f"{KEY}+VALE20")
    assert selection.resolve_link(org, pair, {KEY: "https://meli.la/abc"}) == (
        "https://meli.la/abc")


@pytest.mark.parametrize("coupon, reason", [
    (code(minimum=Decimal(900)), "abaixo_do_minimo"),
    (code(until=NOW.date() - timedelta(days=1)), "cupom_invalido"),
    (code(value=Decimal(0)), "cupom_invalido"),
    (code(value=Decimal(95)), "cupom_invalido"),
    (code(kind="amount", value=Decimal(500)), "cupom_invalido"),
    (code(code="COM ESPACO"), "cupom_invalido"),
    (code(code="<b>x</b>"), "cupom_invalido"),
])
def test_a_coupon_that_does_not_hold_up_is_refused_before_saving(org, ml, coupon, reason):
    [out] = add(org, ML_URL, coupon).outcomes
    assert (out.status, out.reason) == ("erro", reason)
    assert not Offer.objects.exists()


# ── Shopee ────────────────────────────────────────────────────────────────
NODE = {
    "itemId": "40856700906", "productName": "Fone Bluetooth TWS Pro com Cancelamento de Ruído",
    "productLink": "https://shopee.com.br/product/1006215031/40856700906",
    "offerLink": "https://s.shopee.com.br/AbCd123",
    "imageUrl": "https://down-br.img.susercontent.com/file/br-11134207-abc",
    "priceMin": "70.00", "priceMax": "70.00", "priceDiscountRate": 30,
    "commissionRate": "0.08", "sales": 540, "ratingStar": "4.9", "shopId": "1006215031",
}
SHOPEE_URL = "https://shopee.com.br/product/1006215031/40856700906"


@pytest.fixture
def shop(settings, monkeypatch):
    settings.SHOPEE_APP_ID, settings.SHOPEE_APP_SECRET = "app123", "secret456"
    state = {"nodes": [NODE], "queries": []}

    def execute(query, variables=None, *, app_id, secret, http=None):
        state["queries"].append(query)
        return {"productOfferV2": {"nodes": state["nodes"]}}

    monkeypatch.setattr(shopee, "execute", execute)
    return state


def test_a_shopee_product_comes_with_the_commissioned_link(org, shop):
    [out] = add(org, SHOPEE_URL).outcomes
    assert (out.status, out.store, out.key) == ("ok", "shopee", "shp:40856700906")
    offer = Offer.objects.get(store="shopee")
    assert (offer.price_to, offer.commission_pct) == (Decimal("70.00"), Decimal("8.00"))
    assert AffiliateLink.objects.for_org(org).get(product_key="shp:40856700906").affiliate_url == (
        "https://s.shopee.com.br/AbCd123")
    assert "itemId: 40856700906" in shop["queries"][0]  # literal inteiro: sem injeção


def test_shopee_failures_have_their_own_reasons(org, shop, settings):
    shop["nodes"] = []
    assert add(org, SHOPEE_URL).outcomes[0].reason == "nao_encontrado"
    shop["nodes"] = [{**NODE, "ratingStar": "3.0"}]
    assert add(org, SHOPEE_URL).outcomes[0].reason == "nao_atende"
    settings.SHOPEE_APP_SECRET = ""
    assert add(org, SHOPEE_URL).outcomes[0].reason == "shopee_desligada"
    assert not Offer.objects.exists()


def test_a_shopee_api_failure_is_reported_not_raised(org, shop, monkeypatch):
    def boom(*args, **kwargs):
        raise shopee.ShopeeError("A Shopee demorou demais.", retryable=True)

    monkeypatch.setattr(shopee, "execute", boom)
    assert add(org, SHOPEE_URL).outcomes[0].reason == "shopee_falhou"


def test_a_coupon_on_another_store_is_refused(org, shop):
    [out] = add(org, SHOPEE_URL, code()).outcomes
    assert (out.status, out.reason) == ("erro", "cupom_so_ml")
    assert not Offer.objects.exists()


# ── Amazon ────────────────────────────────────────────────────────────────
class AmazonHttp:
    def __init__(self, html="", status=200):
        self.html, self.status, self.terms = html, status, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.terms.append(params["k"])
        return SimpleNamespace(status_code=self.status, text=self.html)


@pytest.fixture
def tagged(org):
    org.amazon_tag = "luizahfn-20"
    org.save()
    return org


AMAZON_URL = "https://www.amazon.com.br/Air-Fryer/dp/B0CH5PRZH9/ref=sr_1_1"


def test_an_amazon_product_is_read_from_the_search_by_asin(tagged):
    http = AmazonHttp(amazon_page(amazon_card(asin="B000000001"), amazon_card()))
    [out] = add(tagged, AMAZON_URL, http=http).outcomes
    assert (out.status, out.store, out.key) == ("ok", "amazon", "B0CH5PRZH9")
    assert http.terms == ["B0CH5PRZH9"]
    offer = Offer.objects.get(store="amazon")
    assert (offer.product_key, offer.source, offer.price_to) == (
        "B0CH5PRZH9", amazon.SOURCE, Decimal("542.71"))
    assert not Offer.objects.filter(product_key="B000000001").exists()  # só o que foi pedido


def test_amazon_needs_the_tag_the_switch_and_a_clean_page(org, tagged, settings):
    good = AmazonHttp(amazon_page(amazon_card()))
    assert add(org, AMAZON_URL, http=good).outcomes[0].status == "ok"
    Offer.objects.all().delete()
    assert add(org, AMAZON_URL, http=AmazonHttp(amazon_page(amazon_card(asin="B000000001")))
               ).outcomes[0].reason == "nao_encontrado"
    assert add(org, AMAZON_URL, http=AmazonHttp(amazon_page(amazon_card(struck="")))
               ).outcomes[0].reason == "nao_encontrado"  # o cartão sem "De" nem é lido
    assert add(org, AMAZON_URL, http=AmazonHttp(amazon_page(amazon_card(rating="3,0")))
               ).outcomes[0].reason == "nao_atende"
    assert add(org, AMAZON_URL, http=AmazonHttp("captcha", 200)).outcomes[0].reason == (
        "amazon_bloqueou")
    settings.AMAZON_COLLECT = False
    assert add(org, AMAZON_URL, http=AmazonHttp()).outcomes[0].reason == "amazon_desligada"
    settings.AMAZON_COLLECT = True
    org.amazon_tag = ""
    org.save()
    assert add(org, AMAZON_URL, http=AmazonHttp()).outcomes[0].reason == "sem_tag"


# ── lote ──────────────────────────────────────────────────────────────────
def test_each_line_has_its_own_outcome_and_the_extra_lines_are_counted(org, ml, shop):
    amazon_lines = "\n".join(f"https://www.amazon.com.br/dp/B0CH5PR{n:03d}" for n in range(20))
    result = add(org, f"{ML_URL}\n{SHOPEE_URL}\nnão é link\nhttps://meli.la/x\n{amazon_lines}")
    assert len(result.outcomes) == manual.MAX_LINES and result.ignored == 4
    assert [(o.status, o.reason) for o in result.outcomes[:4]] == [
        ("sem_link", ""), ("ok", ""), ("erro", "link_invalido"), ("erro", "link_curto")]
    assert {o.reason for o in result.outcomes[4:]} == {"sem_tag"}  # org sem tag, sem rede


def test_a_slow_batch_stops_before_the_web_worker_is_killed(org, ml):
    ticks = iter([0, 1, 5, 19, 21, 40])  # o relógio anda a cada linha processada

    result = manual.add(org, "\n".join([ML_URL] * 5), now=NOW, clock=lambda: next(ticks),
                        deadline=20)
    assert [(o.status, o.reason) for o in result.outcomes] == [
        ("sem_link", ""), ("sem_link", ""), ("sem_link", ""),
        ("erro", "tempo_esgotado"), ("erro", "tempo_esgotado")]
    assert Offer.objects.count() == 1  # a mesma oferta três vezes: uma linha só
