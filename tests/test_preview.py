from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.delivery.models import OfferDelivery, OutboxItem
from apps.delivery.outbox import enqueue_specific, preview_specific
from apps.offers import ml_api
from apps.offers.models import AffiliateLink, Offer, Rule
from tests.conftest import NOW

pytestmark = pytest.mark.django_db

IMG = "https://http2.mlstatic.com/D_NQ_NP_1.webp"


@pytest.fixture(autouse=True)
def api(monkeypatch):
    state = {"price": {"price": 500.0, "price_from": 1000.0, "free_shipping": True,
                       "official_store": True}}
    monkeypatch.setattr(ml_api, "product_price", lambda pid, http=None: state["price"])
    monkeypatch.setattr("apps.delivery.outbox.fetch_image_jpeg", lambda url: b"jpeg")
    return state


@pytest.fixture
def rule(org, destination):
    return Rule.unscoped.create(org=org, destination=destination, name="Teste", window_start=0,
                                window_end=0, active=False)


def ml_offer(**over):
    base = dict(
        store="mercadolivre", source="v1-catalogo", product_key="MLB1", link_key="MLB1",
        title="Air Fryer Oven 12L WAP", url="https://www.mercadolivre.com.br/p/MLB1",
        image_url=IMG, price_from=Decimal(900), price_to=Decimal(450), discount_pct=Decimal(50),
        valid_on=NOW.date(), last_seen=NOW)
    base.update(over)
    return Offer.objects.create(**base)


def link(org, key="MLB1"):
    return AffiliateLink.unscoped.create(org=org, product_key=key,
                                         affiliate_url="https://meli.la/abc")


def test_the_preview_is_exactly_what_the_send_would_queue(org, rule):
    offer = ml_offer()
    link(org)
    text, reason = preview_specific(org, rule, offer, NOW)
    assert reason == ""
    assert text == (
        "🛍️ *Air Fryer Oven 12L WAP*\n\nDe ❌ ~R$ 1.000~\nPor 🔥 *R$ 500* (50% OFF)\n"
        "🚚 Frete grátis · 🏪 Loja oficial\n\n🔗 https://meli.la/abc")
    assert not OutboxItem.unscoped.exists() and not OfferDelivery.unscoped.exists()
    item, _reason = enqueue_specific(org, rule, Offer.objects.get(id=offer.id), NOW)
    assert item.body == text  # a mesma legenda, linha a linha


def test_the_preview_says_why_it_would_not_go_and_still_shows_the_stored_text(org, rule, api):
    offer = ml_offer()
    text, reason = preview_specific(org, rule, offer, NOW)
    assert reason == "sem_link_do_dono" and "(sem link do dono)" in text
    assert "Por 🔥 *R$ 450*" in text  # sem prova: os dados guardados, não os de agora
    link(org)
    for change, expected in [
        (lambda: Offer.objects.filter(id=offer.id).update(
            coupon_kind="code", coupon_code="X", coupon_until=NOW - timedelta(hours=1)),
         "cupom_vencido"),
        (lambda: Offer.objects.filter(id=offer.id).update(
            coupon_kind="", coupon_until=None, last_seen=NOW - timedelta(days=20)),
         "observacao_velha"),
        (lambda: (Offer.objects.filter(id=offer.id).update(last_seen=NOW),
                  api.update(price={})), "preco_inconclusivo"),
    ]:
        change()
        assert preview_specific(org, rule, Offer.objects.get(id=offer.id), NOW)[1] == expected


def test_a_group_with_a_message_waiting_blocks_the_send_and_the_preview_says_so(
        org, destination, rule):
    offer = ml_offer()
    link(org)
    enqueue_specific(org, rule, offer, NOW)
    other = ml_offer(product_key="MLB2", link_key="MLB2")
    link(org, "MLB2")
    assert preview_specific(org, rule, other, NOW)[1] == "grupo_indisponivel"


def test_without_a_group_only_the_offer_itself_is_checked(org):
    offer = ml_offer()
    link(org)
    text, reason = preview_specific(org, None, offer, NOW)
    assert reason == "" and "Frete grátis" in text


def test_the_preview_does_not_make_a_new_shopee_short_link(org, rule, settings, monkeypatch):
    from apps.offers import shopee

    settings.SHOPEE_TRACK = True
    calls = []
    monkeypatch.setattr(shopee, "tracked_link", lambda *a, **k: calls.append(a) or "x")
    offer = Offer.objects.create(
        store="shopee", source="shopee-api", product_key="shp:1", link_key="shp:1",
        title="Fone Bluetooth TWS Pro", url="https://shopee.com.br/product/1/2", image_url=IMG,
        price_from=Decimal(100), price_to=Decimal(70), discount_pct=Decimal(30),
        valid_on=NOW.date(), last_seen=NOW, rating=Decimal("4.9"), sales=540, seller="Loja Boa")
    AffiliateLink.unscoped.create(org=org, product_key="shp:1",
                                  affiliate_url="https://s.shopee.com.br/AbC")
    text, reason = preview_specific(org, rule, offer, NOW)
    assert reason == "" and calls == []
    assert "⭐ 4,9 · 540 vendidos" in text and text.endswith("🔗 https://s.shopee.com.br/AbC")


# ── a página ────────────────────────────────────────────────────────────────
@pytest.fixture
def staff(client, monkeypatch, django_user_model):
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)
    client.force_login(django_user_model.objects.create_user("dono", password="x", is_staff=True))


def page(client, offer, **query):
    return client.get(reverse("panel:offer_preview", args=[offer.id]), query)


def test_the_preview_page_requires_staff(client, org):
    assert client.get(reverse("panel:offer_preview", args=[1])).status_code == 302


def test_the_page_shows_the_photo_and_the_caption_like_whatsapp_does(client, staff, org, rule):
    offer = ml_offer(title="Air Fryer <b>Oven</b> 12L")
    link(org)
    html = page(client, offer).content.decode()
    assert f'src="{IMG}"' in html and "<strong>R$ 500</strong>" in html
    assert "<s>R$ 1.000</s>" in html and "(50% OFF)" in html
    assert "<b>Oven</b>" not in html and "&lt;b&gt;Oven&lt;/b&gt;" in html  # nada de HTML solto
    assert reverse("panel:offer_send", args=[offer.id]) in html and 'name="rule"' in html


def test_the_page_names_the_blocker_and_hides_the_send_button(client, staff, org, rule):
    offer = ml_offer()
    html = page(client, offer).content.decode()
    assert "Sem link seu" in html and "(sem link do dono)" in html
    assert 'class="btn btn-accent"' not in html  # sem botão de enviar quando não sai


def test_the_page_uses_the_chosen_group(client, staff, org, destination, rule):
    other = Rule.unscoped.create(org=org, destination=destination, name="Outro", active=False)
    offer = ml_offer()
    link(org)
    html = page(client, offer, rule=other.id).content.decode()
    assert f'<option value="{other.id}" selected>' in html
    assert page(client, offer, rule="999").status_code == 200  # regra que não existe: a primeira


def test_a_missing_offer_is_a_404(client, staff, org):
    assert client.get(reverse("panel:offer_preview", args=[999999])).status_code == 404


def test_the_row_actions_link_to_the_preview(client, staff, org, rule):
    ml_offer()
    link(org)
    html = client.get(reverse("panel:offers")).content.decode()
    assert "/previa/" in html and 'aria-label="Prévia"' in html
