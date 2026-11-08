from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.offers import ml_api
from apps.offers.models import AffiliateLink, Offer, Rule
from tests.conftest import NOW, FakeTransport

pytestmark = pytest.mark.django_db

KEY = "MLB42225245"
ML_URL = f"https://www.mercadolivre.com.br/air-fryer/p/{KEY}"
IMG = "https://http2.mlstatic.com/D_NQ_NP_898071-MLA99545431524_122025-F.jpg"
VALID = (NOW + timedelta(days=5)).date().isoformat()


@pytest.fixture(autouse=True)
def api(monkeypatch):
    state = {"info": {"name": "Air Fryer Oven 12L WAP", "image": IMG, "status": "active"},
             "price": {"price": 500.0, "price_from": 800.0, "winner_tagged": True}}
    monkeypatch.setattr(ml_api, "product", lambda pid, http=None: state["info"])
    monkeypatch.setattr(ml_api, "product_price", lambda pid, http=None: state["price"])
    monkeypatch.setattr("apps.delivery.outbox.fetch_image_jpeg", lambda url: b"jpeg")
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)
    return state


@pytest.fixture
def staff(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("dono", password="x", is_staff=True))


@pytest.fixture
def rule(org, destination):
    return Rule.unscoped.create(org=org, destination=destination, name="Teste ofertas",
                                window_start=0, window_end=0, active=False)


def post(client, **data):
    return client.post(reverse("panel:manual"), data).content.decode()


def test_the_page_requires_staff(client, org):
    assert client.get(reverse("panel:manual")).status_code == 302
    assert client.post(reverse("panel:manual"), {"links": ML_URL}).status_code == 302
    assert not Offer.objects.exists()


def test_the_form_and_the_menu_item(client, staff, org):
    html = client.get(reverse("panel:manual")).content.decode()
    for name in ("links", "code", "tipo", "valor", "minimo", "teto", "validade"):
        assert f'name="{name}"' in html
    assert "Adicionar" in html and reverse("panel:manual") in html and "Resultado" not in html


def test_each_line_shows_its_state(client, staff, org, rule):
    AffiliateLink.unscoped.create(org=org, product_key=KEY, affiliate_url="https://meli.la/abc")
    other = "https://www.mercadolivre.com.br/outro/p/MLB99"
    html = post(client, links=f"{ML_URL}\n{other}\nhttps://meli.la/curto\nhttps://x.io/y")
    assert html.count('badge-green">Pronto<') == 1  # o produto com link do dono
    assert html.count('badge-yellow">Sem link<') == 1  # o outro entrou, falta o link
    assert 'badge-red">Link curto<' in html and 'badge-red">Loja desconhecida<' in html


def test_a_ready_row_sends_through_the_same_endpoint_as_the_offers_page(
        client, staff, org, rule, monkeypatch):
    AffiliateLink.unscoped.create(org=org, product_key=KEY, affiliate_url="https://meli.la/abc")
    html = post(client, links=ML_URL)
    offer = Offer.objects.get(product_key=KEY)
    assert reverse("panel:offer_send", args=[offer.id]) in html
    assert 'name="next" value="/painel/manual/"' in html
    assert f'name="rule" value="{rule.id}"' in html
    transport = FakeTransport()
    monkeypatch.setattr("apps.panel.catalog_views.WahaTransport", lambda **kw: transport)
    sent = client.post(reverse("panel:offer_send", args=[offer.id]),
                       {"rule": rule.id, "next": "/painel/manual/"}, follow=True)
    assert "Enviado." in sent.content.decode()
    assert len(transport.images) == 1 and "https://meli.la/abc" in transport.images[0][2]


def test_a_row_without_the_owner_link_points_to_the_links_page(client, staff, org, rule):
    html = post(client, links=ML_URL)
    assert 'badge-yellow">Sem link<' in html and reverse("panel:links") in html
    assert "Enviar" not in html.split("<tbody>")[1]
    pasted = post(client, links=f"{ML_URL} https://meli.la/zzz")
    assert 'badge-green">Pronto<' in pasted and "Enviar" in pasted.split("<tbody>")[1]


def test_a_coupon_makes_a_pair_and_the_row_shows_the_price_after_it(client, staff, org, rule):
    AffiliateLink.unscoped.create(org=org, product_key=KEY, affiliate_url="https://meli.la/abc")
    html = post(client, links=ML_URL, code="vale20", tipo="pct", valor="20", minimo="100",
                teto="80", validade=VALID)
    pair = Offer.objects.get(link_key=f"{KEY}+VALE20")
    assert (pair.coupon_code, pair.price_to) == ("VALE20", Decimal("420.00"))
    assert "R$ 420" in html and 'badge-green">Pronto<' in html  # código: o link do produto basta
    assert "<details" in html and " open" in html  # o cupom preenchido continua aberto


def test_brazilian_numbers_and_the_amount_type(client, staff, org):
    post(client, links=ML_URL, code="DEZ", tipo="amount", valor="50,50", minimo="1.000,00",
         validade=VALID)
    assert not Offer.objects.exists()  # mínimo de R$ 1.000 acima do preço: o cupom não vale
    post(client, links=ML_URL, code="DEZ", tipo="amount", valor="50,50", minimo="1,5",
         validade=VALID)
    pair = Offer.objects.get(link_key=f"{KEY}+DEZ")
    assert (pair.coupon_amount, pair.coupon_min) == (Decimal("50.50"), Decimal("1.5"))


@pytest.mark.parametrize("fields, message", [
    ({"code": "VALE"}, "Falta o valor do cupom."),
    ({"valor": "20"}, "Falta a validade do cupom."),
    ({"valor": "20", "validade": "13/11/2026"}, "Falta a validade do cupom."),
    ({"valor": "vinte", "validade": VALID}, "Número inválido."),
    ({"valor": "20", "validade": VALID, "minimo": "-5"}, "Número inválido."),
    ({"valor": "20", "validade": VALID, "teto": "1e400"}, "Número inválido."),
])
def test_a_half_filled_coupon_stops_before_anything_is_saved(client, staff, org, fields, message):
    html = post(client, links=ML_URL, **fields)
    assert message in html and not Offer.objects.exists()
    assert f">{ML_URL}</textarea>" in html  # o que foi colado continua lá


def test_an_empty_paste_asks_for_a_link(client, staff, org):
    assert "Cole ao menos um link." in post(client, links="  \n ")


def test_extra_lines_are_counted_in_a_warning(client, staff, org):
    lines = "\n".join(f"https://x.io/{n}" for n in range(23))
    html = post(client, links=lines)
    assert "3 linhas ficaram de fora (máx. 20)." in html


def test_what_was_pasted_never_reaches_the_page_unescaped(client, staff, org):
    html = post(client, links='<img src=x onerror=alert(1)>\nhttps://evil.example.com/"><script>')
    assert "<img src=x" not in html and "&lt;img src=x" in html
    assert "<script>alert" not in html and "onerror=alert(1)>" not in html.replace("&gt;", "")


def test_the_pasted_text_is_capped(client, staff, org):
    html = post(client, links="https://x.io/a\n" + "x" * 50000)
    assert html.count("x" * 100) <= 200 and "Loja desconhecida" in html
