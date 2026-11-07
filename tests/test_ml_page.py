from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from apps.coupons.models import CampaignCoupon
from apps.offers import ml_page
from apps.offers.models import Offer, TermRun
from tests.conftest import NOW

pytestmark = pytest.mark.django_db

# Recorte da página real de 07/11/2026: 3 campanhas e 7 cartões da galeria (1 no Pix, 1 sem /p/).
HTML = (Path(__file__).parent / "fixtures" / "ml_cupons.html").read_text(encoding="utf-8")
FIVE = {"MLB42225245", "MLB75828998", "MLB54357048", "MLB29760898", "MLB24047848"}
CONTAINER = "https://lista.mercadolivre.com.br/_Container_13512667?coupon_campaign_id=13512667"


class Http:
    def __init__(self, html=HTML, status=200, url=ml_page.URL, error=None):
        self.html, self.status, self.url, self.error, self.calls = html, status, url, error, 0

    def get(self, url, headers=None, timeout=None, allow_redirects=True):
        self.calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.status, text=self.html, url=self.url)


@pytest.fixture(autouse=True)
def switch_on(settings):
    settings.ML_PUBLIC_PAGES = True


def test_parse_coupons_reads_the_campaigns_of_the_real_page():
    rows = {row["campaign_id"]: row for row in ml_page.parse_coupons(HTML, NOW)}
    assert len(rows) == 3
    fixed = rows["13512667"]
    assert (fixed["amount"], fixed["pct"], fixed["min"], fixed["cap"]) == (
        Decimal("10.00"), None, Decimal("11.00"), Decimal("10.00"))
    assert (fixed["category"], fixed["mode"], fixed["items"]) == ("FASHION", "FIXED", 1171)
    assert fixed["container"] == CONTAINER  # sem o fragmento de rastreio
    assert fixed["expires"].isoformat() == "2026-12-11T02:59:00+00:00"
    percent = rows["13968829"]
    assert (percent["pct"], percent["amount"], percent["cap"]) == (
        Decimal("80"), None, Decimal("110.00"))
    assert percent["category"] == "Para Bebês"


def test_expired_or_inactive_campaigns_are_skipped():
    assert ml_page.parse_coupons(HTML, NOW + timedelta(days=90)) == []
    assert ml_page.parse_coupons(HTML.replace('"ACTIVE"', '"PAUSED"'), NOW) == []


def test_parse_offers_keeps_catalog_products_at_the_cash_price_only():
    rows = {row["product"]: row for row in ml_page.parse_offers(HTML)}
    assert set(rows) == FIVE  # o Switch "no Pix" e o anúncio sem /p/ ficam de fora
    air = rows["MLB42225245"]
    assert (air["price"], air["before"]) == (Decimal("456.90"), Decimal("849.86"))
    assert air["title"].startswith("Air Fryer Oven 12L WAP")
    assert air["image"].startswith("https://http2.mlstatic.com/D_NQ_NP_")
    assert air["image"].endswith("-F.webp")


def test_unreadable_page_gives_nothing():
    assert ml_page.bricks("") == [] and ml_page.bricks("<html></html>") == []
    broken = '<script id="__NORDIC_RENDERING_CTX__">_n.ctx.r={"a":</script>'
    assert ml_page.bricks(broken) == []
    assert ml_page.parse_coupons(broken, NOW) == [] and ml_page.parse_offers(broken) == []


def test_collect_stores_the_campaigns_and_the_offers_of_the_day():
    assert ml_page.collect(NOW, http=Http()) == {"campanhas": 3, "ofertas": 5}
    assert CampaignCoupon.objects.count() == 3
    campaign = CampaignCoupon.objects.get(campaign_id="13512667")
    assert (campaign.title, campaign.discount_amount, campaign.total_items) == (
        "R$ 10 OFF", Decimal("10.00"), 1171)
    assert campaign.container_url == CONTAINER
    offer = Offer.objects.get(product_key="MLB42225245")
    assert (offer.store, offer.source, offer.link_key) == (
        "mercadolivre", ml_page.SOURCE, "MLB42225245")
    assert offer.url == "https://www.mercadolivre.com.br/p/MLB42225245"
    assert (offer.price_from, offer.price_to, offer.discount_pct) == (
        Decimal("849.86"), Decimal("456.90"), Decimal("46.24"))
    assert offer.valid_on == NOW.date() and offer.image_url.endswith("-F.webp")
    assert set(Offer.objects.values_list("product_key", flat=True)) == FIVE


def test_a_product_the_api_already_proved_today_is_not_overwritten():
    Offer.objects.create(
        store="mercadolivre", source="ml-api-catalogo", product_key="MLB42225245",
        link_key="MLB42225245", title="Air", url="https://www.mercadolivre.com.br/p/MLB42225245",
        image_url="https://http2.mlstatic.com/a.jpg", price_from=Decimal("900"),
        price_to=Decimal("400"), discount_pct=Decimal("55.56"), valid_on=NOW.date(),
        last_seen=NOW)
    assert ml_page.collect(NOW, http=Http()) == {"campanhas": 3, "ofertas": 4}
    offer = Offer.objects.get(product_key="MLB42225245")
    assert (offer.source, offer.price_to) == ("ml-api-catalogo", Decimal("400"))


def test_a_prohibited_title_never_becomes_an_offer():
    html = HTML.replace("Berço Portátil", "Remédio Portátil")
    assert ml_page.collect(NOW, http=Http(html)) == {"campanhas": 3, "ofertas": 4}
    assert not Offer.objects.filter(product_key="MLB54357048").exists()


def test_once_an_hour_and_the_next_load_refreshes_the_same_rows():
    http = Http()
    ml_page.collect(NOW, http=http)
    assert ml_page.collect(NOW + timedelta(minutes=30), http=http) == {} and http.calls == 1
    assert ml_page.collect(NOW + timedelta(minutes=61), http=http)["ofertas"] == 5
    assert http.calls == 2 and Offer.objects.count() == 5 and CampaignCoupon.objects.count() == 3


@pytest.mark.parametrize("http", [
    Http(status=503),
    Http(url="https://www.mercadolivre.com.br/gz/account-verification?go=x"),
    Http(error=requests.exceptions.ConnectTimeout())])
def test_a_wall_or_an_outage_stores_nothing_and_waits_an_hour(http):
    assert ml_page.collect(NOW, http=http) == {}
    assert not Offer.objects.exists() and not CampaignCoupon.objects.exists()
    assert ml_page.collect(NOW + timedelta(minutes=10), http=http) == {}
    assert http.calls == 1 and TermRun.objects.filter(term=ml_page.RUN_KEY).exists()


def test_a_page_with_no_known_blocks_stores_nothing_and_waits_too():
    http = Http("<html>nada</html>")
    assert ml_page.collect(NOW, http=http) == {"campanhas": 0, "ofertas": 0}
    assert ml_page.collect(NOW + timedelta(minutes=5), http=http) == {} and http.calls == 1


def test_utf8_body_is_decoded_even_when_requests_guesses_latin1():
    raw = HTML.encode("utf-8")

    class Latin(Http):
        def get(self, url, **kwargs):
            return SimpleNamespace(status_code=200, content=raw, text=raw.decode("latin-1"),
                                   url=ml_page.URL)

    ml_page.collect(NOW, http=Latin())
    assert CampaignCoupon.objects.get(campaign_id="13968829").category == "Para Bebês"


def test_switch_off(settings):
    settings.ML_PUBLIC_PAGES = False
    http = Http()
    assert ml_page.collect(NOW, http=http) == {} and http.calls == 0
