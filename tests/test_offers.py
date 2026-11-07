from datetime import date, timedelta
from decimal import Decimal

import pytest

from apps.compose.offer import build_offer_message
from apps.delivery.models import OfferDelivery, OutboxItem
from apps.delivery.outbox import deliver_due, enqueue_for_rule
from apps.offers import catalog, ml_api
from apps.offers.feeds import ingest_daily_offers, parse_daily_offers
from apps.offers.keys import product_key
from apps.offers.links import import_pairs, pending_urls, safe_affiliate_url
from apps.offers.models import AffiliateLink, Offer, Rule
from apps.offers.selection import in_window, matches_terms
from tests.conftest import NOW, FakeTransport

TODAY = date(2026, 11, 7)

FEED = """<script>const ITEMS = [
 {"vertical": "Eletrodomésticos", "data_oferta": "07/11/2026",
  "produto": "Caixa De Som Bluetooth Portátil 30w", "url": "https://produto.mercadolivre.com.br/MLB-5650022814",
  "preco_de": "R$399,90", "preco_por": "R$147,90", "cupom": "", "codigo": "", "desconto": "63%"},
 {"vertical": "Móveis", "data_oferta": "07/11/2026", "produto": "Colchão Luuna",
  "url": "https://produto.mercadolivre.com.br/MLB-111111111",
  "preco_de": "R$2.852,50", "preco_por": "R$2.852,50", "desconto": "0%"},
 {"vertical": "Moda", "data_oferta": "06/11/2026", "produto": "Ontem",
  "url": "https://produto.mercadolivre.com.br/MLB-222222222",
  "preco_de": "R$100,00", "preco_por": "R$50,00", "desconto": "50%"},
 {"vertical": "Carros", "data_oferta": "07/11/2026", "produto": "Carro usado barato",
  "url": "https://produto.mercadolivre.com.br/MLB-333333333",
  "preco_de": "R$100,00", "preco_por": "R$50,00", "desconto": "50%"},
 {"vertical": "X", "data_oferta": "07/11/2026", "produto": "Link fora do ML",
  "url": "https://evil.example.com/MLB-444444444",
  "preco_de": "R$100,00", "preco_por": "R$50,00", "desconto": "50%"}
];</script>""".replace("\n ", " ")


def make_offer(key="MLB5650022814", title="Caixa de Som", price=147.90, frm=399.90, pct=63,
               source="ml-ofertas-diarias", seen=NOW):
    return Offer.objects.create(
        product_key=key, title=title, url=f"https://produto.mercadolivre.com.br/{key}",
        price_from=Decimal(str(frm)), price_to=Decimal(str(price)), discount_pct=Decimal(pct),
        valid_on=TODAY, last_seen=seen, source=source,
    )


@pytest.fixture
def rule(org, destination):
    return Rule.unscoped.create(
        org=org, destination=destination, name="Ofertas", type=Rule.OFFERS,
        window_start=8, window_end=20, interval_minutes=60, daily_cap=12,
    )


def link_for(org, key="MLB5650022814"):
    return AffiliateLink.unscoped.create(
        org=org, product_key=key, affiliate_url="https://meli.la/xyz"
    )


def test_product_key_variants():
    assert product_key("https://www.mercadolivre.com.br/p/MLB123456?x=1") == "MLB123456"
    assert product_key("https://produto.mercadolivre.com.br/MLB-5650022814") == "MLB5650022814"
    long_url = "https://produto.mercadolivre.com.br/MLB-5650022814-titulo-_JM"
    assert product_key(long_url) == "MLB5650022814"


def test_parse_daily_offers_filters(db):
    offers = parse_daily_offers(FEED, TODAY)
    titles = [o["title"] for o in offers]
    assert titles == ["Caixa De Som Bluetooth Portátil 30w", "Colchão Luuna"]
    assert offers[0]["discount_pct"] == Decimal("63.02")
    assert offers[1]["discount_pct"] == Decimal("0")


def test_ingest_daily_offers_idempotent(db):
    assert ingest_daily_offers(FEED, NOW) == 2
    assert ingest_daily_offers(FEED, NOW) == 2
    assert Offer.objects.count() == 2


def test_offer_message_golden():
    offer = Offer(title="Caixa de Som", price_from=Decimal("399.90"), price_to=Decimal("147.90"),
                  valid_on=TODAY, coupon_code="")
    assert build_offer_message(offer, "https://meli.la/xyz") == (
        "🛍️ *Caixa de Som*\nDe ❌ ~R$ 399,90~\nPor 🔥 *R$ 147,90*\n⏳ Válido até 07/11\n"
        "🔗 https://meli.la/xyz"
    )
    offer.coupon_code = "VALE10"
    assert "🎟️ CUPOM: VALE10" in build_offer_message(offer, "https://meli.la/xyz")


def test_window_weekdays_and_midnight(rule):
    assert in_window(rule, NOW)  # 12h BRT
    rule.window_start, rule.window_end = 20, 6
    assert not in_window(rule, NOW)
    rule.window_start, rule.window_end = 8, 8
    assert in_window(rule, NOW)
    rule.window_start, rule.window_end, rule.weekdays = 8, 20, "1,2"  # sábado 07/11
    assert not in_window(rule, NOW)


def test_terms_are_accent_insensitive_with_negatives(rule):
    rule.terms, rule.negative_terms = "aspirador robo, robô aspirador", "filtro, refil"
    assert matches_terms(rule, "Robô Aspirador Wap W400")
    assert not matches_terms(rule, "Filtro para aspirador robô")
    assert not matches_terms(rule, "Cafeteira")


def test_no_owner_link_no_offer(org, rule):
    make_offer()
    assert enqueue_for_rule(rule, NOW) is None


def test_offer_enqueued_and_delivered_with_cooldown(org, destination, rule):
    make_offer()
    link_for(org)
    item = enqueue_for_rule(rule, NOW)
    assert item and item.kind == "offer" and "https://meli.la/xyz" in item.body
    deliver_due(FakeTransport(), NOW)
    later = NOW + timedelta(hours=2)
    make_offer(key="MLB999", title="Outro", seen=later)
    link_for(org, "MLB999")
    # mesmo produto não volta (cooldown); o outro sai
    second = enqueue_for_rule(rule, later)
    assert second and "Outro" in second.body
    assert OfferDelivery.objects.for_org(org).count() == 2


def test_rule_interval_and_daily_cap(org, destination, rule):
    rule.daily_cap = 1
    rule.save()
    make_offer()
    link_for(org)
    enqueue_for_rule(rule, NOW)
    deliver_due(FakeTransport(), NOW)
    make_offer(key="MLB2", title="B")
    link_for(org, "MLB2")
    assert enqueue_for_rule(rule, NOW + timedelta(minutes=10)) is None  # intervalo
    assert enqueue_for_rule(rule, NOW + timedelta(hours=2)) is None  # teto diário


def test_min_discount_and_price_range(org, rule):
    make_offer(pct=10)
    link_for(org)
    assert enqueue_for_rule(rule, NOW) is None  # abaixo de 15%
    rule.min_discount_pct, rule.price_max = Decimal(5), Decimal(100)
    rule.save()
    assert enqueue_for_rule(rule, NOW) is None  # preço acima do teto


def test_rule_daily_cap_ceiling():
    assert Rule(daily_cap=99).effective_cap == 30


def test_import_pairs_and_pending(org, db):
    make_offer()
    assert pending_urls(org, NOW) == ["https://produto.mercadolivre.com.br/MLB5650022814"]
    text = (
        "https://produto.mercadolivre.com.br/MLB5650022814 https://meli.la/abc\n"
        "https://produto.mercadolivre.com.br/MLB-1 https://evil.example.com/x\n"
        "lixo sem par\n"
    )
    assert import_pairs(org, text) == {"importados": 1, "rejeitados": 2}
    assert pending_urls(org, NOW) == []
    assert safe_affiliate_url("http://meli.la/x") == ""
    assert safe_affiliate_url("https://meli.la/x") == "https://meli.la/x"


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p


class FakeApi:
    def __init__(self, price=147.90, tagged=True):
        self.price, self.tagged, self.calls = price, tagged, []

    def post(self, url, **kw):
        return FakeResp(200, {"access_token": "tok", "expires_in": 21600})

    def get(self, url, **kw):
        self.calls.append(url)
        if url.endswith("/products/search"):
            return FakeResp(200, {"results": [{"id": "MLB777", "name": "Robô Aspirador X"}]})
        if url.endswith("/products/MLB777/items"):
            tags = ["kvs_primary"] if self.tagged else []
            return FakeResp(200, {"results": [
                {"price": self.price, "original_price": 399.9, "item_id": "MLB1", "tags": tags}]})
        return FakeResp(404, {})


@pytest.fixture
def api_env(settings):
    settings.ML_API_CLIENT_ID, settings.ML_API_CLIENT_SECRET = "id", "secret"
    ml_api.reset_token_cache()
    yield
    ml_api.reset_token_cache()


def test_collect_catalog_builds_offers_for_rule_terms(org, rule, api_env):
    rule.terms = "robo aspirador"
    rule.save()
    assert catalog.collect_catalog(NOW, http=FakeApi()) == 1
    offer = Offer.objects.get(product_key="MLB777")
    assert offer.price_to == Decimal("147.9") and offer.discount_pct == Decimal("63.02")
    assert offer.url == "https://www.mercadolivre.com.br/p/MLB777"


def test_collect_catalog_skips_unproven_winner(org, rule, api_env):
    rule.terms = "robo"
    rule.save()
    assert catalog.collect_catalog(NOW, http=FakeApi(tagged=False)) == 0


def test_ml_api_without_credentials_is_empty(settings):
    settings.ML_API_CLIENT_ID = ""
    ml_api.reset_token_cache()
    assert ml_api.search_products("x", http=FakeApi()) == []


def test_catalog_offer_is_revalidated_before_enqueue(org, rule, api_env, monkeypatch):
    make_offer(key="MLB777", title="Robô", source=catalog.SOURCE)
    link_for(org, "MLB777")
    monkeypatch.setattr(ml_api, "product_price", lambda pid, http=None: {"price": 160.0})
    assert enqueue_for_rule(rule, NOW) is None  # preço mudou
    monkeypatch.setattr(ml_api, "product_price", lambda pid, http=None: {"price": 147.9})
    assert enqueue_for_rule(rule, NOW)


def test_coupon_notice_rule_uses_rule_gates(org, destination, container_link):
    from apps.coupons.models import Coupon

    Coupon.objects.create(
        code="ABC", source="t", valid_until=date(2026, 11, 15), last_seen=NOW,
        container_url=container_link.container_url,
    )
    notice = Rule.unscoped.create(
        org=org, destination=destination, name="Aviso", type=Rule.COUPON_NOTICE,
        window_start=20, window_end=22,
    )
    assert enqueue_for_rule(notice, NOW) is None  # fora da janela (12h)
    notice.window_start, notice.window_end = 8, 20
    notice.save()
    item = enqueue_for_rule(notice, NOW)
    assert item and item.rule_id == notice.id
    assert OutboxItem.objects.for_org(org).count() == 1
