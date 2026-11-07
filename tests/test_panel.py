from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils.html import escape

from apps.delivery.models import OutboxItem
from apps.delivery.outbox import enqueue_for_rule
from apps.offers import ml_api, niches
from apps.offers.models import AffiliateLink, Rule
from apps.orgs.models import Destination, Organization
from tests.conftest import NOW, FakeTransport, transient
from tests.test_offers import make_offer

GROUPS = [
    {"id": "111@g.us", "name": "Promo Casa", "size": 120},
    {"id": "222@g.us", "name": "Achados Tech", "size": 80},
    {"id": "120363@g.us", "name": "Teste ofertas", "size": 3},
]


class Groups:
    def __init__(self, rows):
        self.rows = rows

    def list_groups(self):
        return self.rows

    def session_status(self, timeout=None):
        return "WORKING"


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)


@pytest.fixture
def staff(client, frozen, django_user_model):
    user = django_user_model.objects.create_user("dono", password="x", is_staff=True)
    client.force_login(user)
    return user


@pytest.fixture
def rule(org, destination):
    return Rule.unscoped.create(org=org, destination=destination, name="Teste ofertas",
                                window_start=0, window_end=0, niches=[], active=False)


@pytest.fixture
def sendable(org, monkeypatch):
    monkeypatch.setattr(ml_api, "product_price", lambda pid, http=None: {"price": 100.0})
    monkeypatch.setattr("apps.delivery.outbox.fetch_image_jpeg", lambda url: b"jpeg")
    make_offer()
    AffiliateLink.unscoped.create(org=org, product_key="MLB111",
                                  affiliate_url="https://meli.la/abc")


def amazon_without_tag():
    return make_offer(key="B012345678", store="amazon", title="Cafeteira",
                      url="https://www.amazon.com.br/dp/B012345678")


def page(client, name, *args, **query):
    return client.get(reverse(f"panel:{name}", args=args), query).content.decode()


def post_config(client, rule, **over):
    data = {"stores": ["mercadolivre"], "daily_cap": "12", "window_start": "8",
            "window_end": "20", "min_discount": "15", "interval": "60"}
    data.update(over)
    return client.post(reverse("panel:group", args=[rule.id]), data)


# ── acesso ────────────────────────────────────────────────────────────────
def test_panel_requires_staff(client, org, rule, django_user_model):
    urls = [reverse("panel:groups"), reverse("panel:add"), reverse("panel:group", args=[rule.id])]
    for url in urls:
        response = client.get(url)
        assert response.status_code == 302 and "/admin/login/" in response["Location"]
    client.force_login(django_user_model.objects.create_user("comum", password="x"))
    for url in urls:
        assert client.get(url).status_code == 302
    assert client.post(reverse("panel:send_now", args=[rule.id])).status_code == 302
    assert OutboxItem.unscoped.count() == 0


def test_panel_posts_require_csrf_token(org, rule, staff, django_user_model):
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    strict.force_login(staff)
    for url in [reverse("panel:add"), reverse("panel:group", args=[rule.id]),
                reverse("panel:send_now", args=[rule.id])]:
        assert strict.post(url, {"group": "x"}).status_code == 403, url
    assert OutboxItem.unscoped.count() == 0 and Rule.unscoped.count() == 1


def test_group_names_from_whatsapp_are_escaped(client, staff, org, monkeypatch):
    evil = '<img src=x onerror=alert(1)>"\'&'
    monkeypatch.setattr("apps.panel.views.WahaTransport",
                        lambda: Groups([{"id": "666@g.us", "name": evil, "size": 1}]))
    html = page(client, "add")
    assert evil not in html and "&lt;img src=x onerror=alert(1)&gt;" in html
    client.post(reverse("panel:add"), {"group": "666@g.us"})
    rule = Rule.unscoped.get(destination__external_id="666@g.us")
    for html in (page(client, "groups"), page(client, "group", rule.id)):
        assert evil not in html and "&lt;img src=x onerror=alert(1)&gt;" in html


def test_other_orgs_rule_is_invisible(client, staff, org, rule):
    other = Organization.objects.create(name="Outra", slug="outra")
    dest = Destination.unscoped.create(org=other, kind=Destination.WA_GROUP,
                                       external_id="999@g.us", name="Grupo alheio")
    foreign = Rule.unscoped.create(org=other, destination=dest, name="Grupo alheio")
    assert client.get(reverse("panel:group", args=[foreign.id])).status_code == 404
    assert client.post(reverse("panel:send_now", args=[foreign.id])).status_code == 404
    assert "Grupo alheio" not in page(client, "groups")


# ── lista de grupos ───────────────────────────────────────────────────────
def test_groups_page_empty_state_points_to_add(client, staff, org):
    html = page(client, "groups")
    assert "Nenhum grupo." in html and reverse("panel:add") in html


def test_groups_page_shows_state_niches_and_today_count(client, staff, org, destination, rule):
    cat = niches.catalog()
    keys = [cat[0][1], cat[1][1], cat[2][1]]
    rule.niches = keys
    rule.save()
    OutboxItem.unscoped.create(
        org=org, destination=destination, rule=rule, body="x", kind="offer",
        state=OutboxItem.CONFIRMED, idempotency_key="k1", next_attempt_at=NOW, created_at=NOW,
    )
    html = page(client, "groups")
    assert "Teste ofertas" in html and "Pausado" in html
    assert f"1/{rule.effective_cap}" in html
    assert escape(cat[0][0]) in html and escape(cat[1][0]) in html and "+1" in html
    rule.active = True
    rule.niches = []
    rule.save()
    html = page(client, "groups")
    assert "Ativo" in html and ">Tudo<" in html


def test_groups_page_shows_the_whatsapp_session(client, staff, org, monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr("apps.panel.views.WahaTransport", lambda: transport)
    assert "WhatsApp · Conectado" in page(client, "groups")
    transport.status = "SCAN_QR_CODE"
    html = page(client, "groups")
    assert "WhatsApp · Ler o QR" in html and "badge-yellow" in html
    transport.status = ""
    assert "WhatsApp · Sem resposta" in page(client, "groups")


def test_groups_page_shows_link_builder_state_only_when_there_is_one(client, staff, org):
    from apps.linkbuilder.models import BuilderState

    assert "Link Builder" not in page(client, "groups")
    state = BuilderState.unscoped.create(
        org=org, state=BuilderState.NO_SESSION, detail="rode tools/ml_login.py", updated_at=NOW)
    html = page(client, "groups")
    assert "Link Builder · Sem sessão" in html and "rode tools/ml_login.py" in html
    assert "badge-yellow" in html
    state.state, state.detail = BuilderState.READY, ""
    state.save()
    html = page(client, "groups")
    assert "Link Builder · Pronto" in html and 'badge-yellow">Link Builder' not in html


# ── adicionar grupo ───────────────────────────────────────────────────────
def test_add_page_lists_free_groups_and_filters(client, staff, org, destination, monkeypatch):
    monkeypatch.setattr("apps.panel.views.WahaTransport", lambda: Groups(GROUPS))
    html = page(client, "add")
    assert "Promo Casa" in html and "Achados Tech" in html
    assert "120 membros" in html and "80 membros" in html
    assert 'class="btn btn-accent"' not in html  # uma ação primária por tela: aqui nenhuma
    assert "Teste ofertas" not in html  # já cadastrado
    html = page(client, "add", q="tech")
    assert "Achados Tech" in html and "Promo Casa" not in html
    html = page(client, "add", q="zzz")
    assert "Nenhum grupo encontrado." in html and "Ver tudo" in html


def test_add_creates_inactive_rule_and_opens_it(client, staff, org, monkeypatch):
    monkeypatch.setattr("apps.panel.views.WahaTransport", lambda: Groups(GROUPS))
    response = client.post(reverse("panel:add"), {"group": "111@g.us"})
    dest = Destination.unscoped.get(external_id="111@g.us")
    rule = Rule.unscoped.get(destination=dest)
    assert response.status_code == 302
    assert response["Location"] == reverse("panel:group", args=[rule.id])
    assert dest.org_id == org.id and dest.name == "Promo Casa" and dest.kind == Destination.WA_GROUP
    assert rule.active is False and rule.type == Rule.OFFERS and rule.niches == []


def test_add_rejects_unknown_and_duplicate(client, staff, org, destination, monkeypatch):
    monkeypatch.setattr("apps.panel.views.WahaTransport", lambda: Groups(GROUPS))
    for group_id in ["nao-existe@g.us", "120363@g.us", ""]:
        response = client.post(reverse("panel:add"), {"group": group_id}, follow=True)
        assert "Grupo indisponível." in response.content.decode()
    assert Destination.unscoped.count() == 1 and Rule.unscoped.count() == 0


def test_add_page_offline_offers_pairing(client, staff, org, monkeypatch):
    monkeypatch.setattr("apps.panel.views.WahaTransport", lambda: Groups([]))
    html = page(client, "add")
    assert "WhatsApp sem resposta." in html and 'href="/pair"' in html


# ── configuração do grupo ─────────────────────────────────────────────────
def test_group_page_marks_selected_niches(client, staff, rule):
    macro = "Eletrodomésticos"
    sub = niches.sub_key(macro, "Robô aspirador")
    rule.niches = [sub]
    rule.save()
    html = page(client, "group", rule.id)
    assert f'value="{escape(sub)}" checked' in html
    assert f'value="{escape(niches.macro_key(macro))}" checked' not in html
    assert '<b class="count">1</b>' in html
    for key in niches.valid_keys():
        assert f'value="{escape(key)}"' in html


def test_group_save_keeps_only_valid_choices(client, staff, rule):
    sub = niches.sub_key("Eletrodomésticos", "Robô aspirador")
    macro = niches.macro_key("Automotivo")
    response = post_config(
        client, rule, niches=[sub, macro, "m:Inventado", "s:Casa|Nada"],
        stores=["amazon", "loja-fake", "mercadolivre"], daily_cap="999", window_start="9",
        window_end="21", min_discount="25", interval="45", active="on",
    )
    assert response.status_code == 302
    rule.refresh_from_db()
    assert rule.niches == [sub, macro]
    assert rule.store_list == ["amazon", "mercadolivre"]
    assert (rule.daily_cap, rule.window_start, rule.window_end, rule.interval_minutes) == (
        Rule.DAILY_CEILING, 9, 21, 45)
    assert rule.min_discount_pct == 25 and rule.active is True
    assert "Salvo." in client.get(response["Location"]).content.decode()


def test_group_save_falls_back_on_garbage_numbers(client, staff, rule):
    rule.daily_cap, rule.interval_minutes = 7, 33
    rule.save()
    post_config(client, rule, daily_cap="abc", window_start="-5", window_end="99",
                min_discount="", interval="x", stores=[])
    rule.refresh_from_db()
    assert rule.daily_cap == 7 and rule.interval_minutes == 33
    assert (rule.window_start, rule.window_end) == (0, 23)
    assert rule.min_discount_pct == 15
    assert rule.store_list == ["mercadolivre"]  # nenhuma loja marcada = Mercado Livre
    assert rule.niches == [] and rule.active is False  # caixa desmarcada pausa


def test_group_page_explains_why_nothing_goes_out(client, staff, rule):
    amazon_without_tag()
    html = page(client, "group", rule.id)
    assert "Sem link seu" in html


def test_group_page_lists_what_was_sent_newest_first(client, staff, org, destination, rule):
    def sent(n, at, state=OutboxItem.CONFIRMED, rule_id=rule.id):
        OutboxItem.unscoped.create(
            org=org, destination=destination, rule_id=rule_id, kind="offer", state=state,
            body=f"🛍️ *Produto {n}*\n\nDe ❌ ~R$ 100~\nPor 🔥 *R$ 50*\n\n🔗 https://meli.la/x{n}",
            idempotency_key=f"s{n}", next_attempt_at=at, created_at=at, sent_at=at,
        )

    from datetime import timedelta

    sent(1, NOW - timedelta(hours=2))
    sent(2, NOW - timedelta(minutes=5))
    sent(3, NOW - timedelta(minutes=1), state=OutboxItem.QUEUED)  # ainda na fila: não saiu
    sent(4, NOW - timedelta(minutes=2), rule_id=None)  # sem regra: não é deste grupo
    html = page(client, "group", rule.id)
    assert html.index("Produto 2") < html.index("Produto 1")
    assert "Produto 3" not in html and "Produto 4" not in html
    assert "07/11 11:55" in html and "07/11 10:00" in html  # horário de Brasília


def test_group_page_lists_ready_offers_before_blockers(client, staff, rule, sendable):
    amazon_without_tag()
    html = page(client, "group", rule.id)
    assert html.index("Prontas para sair") < html.index("Sem link seu")


# ── enviar agora ──────────────────────────────────────────────────────────
def test_send_now_is_post_only(client, staff, rule):
    assert client.get(reverse("panel:send_now", args=[rule.id])).status_code == 405


def test_send_now_names_the_top_blocker(client, staff, rule):
    amazon_without_tag()
    response = client.post(reverse("panel:send_now", args=[rule.id]), follow=True)
    assert "Nada publicável agora: Sem link seu." in response.content.decode()
    assert OutboxItem.unscoped.count() == 0


def test_send_now_ignores_window_and_sends(client, staff, org, rule, sendable, monkeypatch):
    rule.window_start, rule.window_end = 1, 2  # 12h em Brasília: fora da janela
    rule.save()
    assert enqueue_for_rule(rule, NOW) is None  # sem o botão, a janela barra
    transport = FakeTransport()
    monkeypatch.setattr("apps.panel.views.WahaTransport", lambda: transport)
    response = client.post(reverse("panel:send_now", args=[rule.id]), follow=True)
    assert "Enviado." in response.content.decode()
    item = OutboxItem.unscoped.get()
    assert item.state == OutboxItem.CONFIRMED and item.rule_id == rule.id
    assert len(transport.images) == 1 and transport.sent == []


def test_send_now_reports_unconfirmed_delivery(client, staff, org, rule, sendable, monkeypatch):
    transport = FakeTransport(transient())
    monkeypatch.setattr("apps.panel.views.WahaTransport", lambda: transport)
    response = client.post(reverse("panel:send_now", args=[rule.id]), follow=True)
    assert "Na fila; envio não confirmado." in response.content.decode()
    assert OutboxItem.unscoped.get().state == OutboxItem.QUEUED


def test_links_page_lists_pending_urls_and_imports_pasted_ones(client, staff, org, rule, sendable):
    rule.active = True  # regra pausada não gera demanda de link
    rule.save()
    make_offer(key="MLB777", price=100, frm=200)  # sem link
    html = page(client, "links")
    assert "https://www.mercadolivre.com.br/p/MLB777" in html and "1 pendentes" in html
    client.post(reverse("panel:links"),
                {"pasted": "https://www.mercadolivre.com.br/p/MLB777 https://meli.la/Z9"})
    assert AffiliateLink.unscoped.get(product_key="MLB777").affiliate_url == "https://meli.la/Z9"
    assert "Nada pendente." in page(client, "links")


def test_links_page_requires_staff(client, org):
    assert client.get(reverse("panel:links")).status_code == 302


def test_group_filters_are_saved_and_cleaned(client, staff, rule):
    post_config(client, rule, coupon_mode="only", price_min="1.250,50", price_max="99,9",
                terms=" robô , aspirador ,, ", negative_terms="usado",
                weekdays=["6", "1", "9", "x"])
    rule.refresh_from_db()
    assert rule.coupon_mode == "only"
    assert (rule.price_min, rule.price_max) == (None, Decimal("99.90"))  # "1.250,50" não é número
    assert rule.terms == "robô, aspirador" and rule.negative_terms == "usado"
    assert rule.weekdays == "1,6"
    post_config(client, rule, coupon_mode="inventado", price_min="500", price_max="100",
                weekdays=["1", "2", "3", "4", "5", "6", "7"])
    rule.refresh_from_db()
    assert rule.coupon_mode == "only"  # valor inválido não troca o modo
    assert (rule.price_min, rule.price_max) == (Decimal("100.00"), Decimal("500.00"))  # inverteu
    assert rule.weekdays == ""  # semana inteira = todos os dias


def test_group_page_shows_the_filters(client, staff, rule):
    rule.coupon_mode, rule.weekdays, rule.terms = "off", "2,3", "robô"
    rule.save()
    html = page(client, "group", rule.id)
    assert '<option value="off" selected>' in html and 'value="robô"' in html
    assert 'name="weekdays" value="2" checked' in html
    assert 'name="weekdays" value="1" checked' not in html


# ── Ofertas e Cupons ──────────────────────────────────────────────────────
def _pair(key="MLB333", code="CODE", until_days=5, **extra):
    from datetime import timedelta as td

    return make_offer(key=key, title=f"Produto {key}", price=60, frm=100, link_key=f"{key}+{code}",
                      coupon_kind="code", coupon_code=code, coupon_pct=Decimal(30),
                      coupon_until=NOW + td(days=until_days), **extra)


def test_catalog_pages_require_staff(client, org):
    for name in ("offers", "coupons"):
        assert client.get(reverse(f"panel:{name}")).status_code == 302
    assert client.post(reverse("panel:offer_send", args=[1])).status_code == 302


def test_coupons_page_lists_only_live_pairs_with_state_and_search(client, staff, org, rule):
    make_offer(key="MLB111", price=100, frm=200)  # sem cupom: fica fora da tela de cupons
    _pair("MLB333", "ALFA")
    _pair("MLB444", "BETA")
    _pair("MLB555", "VELHO", until_days=-1)  # vencido
    AffiliateLink.unscoped.create(org=org, product_key="MLB333+ALFA",
                                  affiliate_url="https://meli.la/a")
    html = page(client, "coupons")
    assert "Produto MLB333" in html and "Produto MLB444" in html
    assert "Produto MLB111" not in html and "VELHO" not in html
    assert "ALFA" in html and "30% OFF" in html and "Com link" in html
    assert "Sem link" in html
    only = page(client, "coupons", estado="com")
    assert "Produto MLB333" in only and "Produto MLB444" not in only
    none = page(client, "coupons", estado="sem")
    assert "Produto MLB444" in none and "Produto MLB333" not in none
    found = page(client, "coupons", q="beta")  # busca por código, sem diferenciar caixa
    assert "Produto MLB444" in found and "Produto MLB333" not in found
    assert "Nada encontrado." in page(client, "coupons", q="zzz") and "Limpar" in page(
        client, "coupons", q="zzz")


def test_offers_page_filters_by_store_and_paginates(client, staff, org, rule):
    for n in range(31):
        make_offer(key=f"MLB{9000 + n}", title=f"Item {n:02d}", price=100, frm=200 + n)
    make_offer(key="B012345678", store="amazon", title="Cafeteira Amazon",
               url="https://www.amazon.com.br/dp/B012345678")
    html = page(client, "offers")
    assert "30+ aqui" in html
    assert "p=2" in html
    assert "Cafeteira Amazon" in page(client, "offers", loja="amazon")
    assert "Item 00" not in page(client, "offers", loja="amazon")
    assert "Sem link" in page(client, "offers", loja="amazon")  # org sem tag: sem link
    org.amazon_tag = "luizahfn-20"
    org.save()
    assert "Com link" in page(client, "offers", loja="amazon")


def test_send_offer_from_the_catalog_uses_the_same_proofs(client, staff, org, rule, sendable,
                                                         monkeypatch):
    from apps.offers.models import Offer

    transport = FakeTransport()
    monkeypatch.setattr("apps.panel.catalog_views.WahaTransport", lambda: transport)
    offer = Offer.objects.get(product_key="MLB111")
    url = reverse("panel:offer_send", args=[offer.id])
    response = client.post(url, {"rule": rule.id, "next": "/painel/ofertas/?loja=mercadolivre"},
                           follow=True)
    assert "Enviado." in response.content.decode()
    assert response.redirect_chain[-1][0] == "/painel/ofertas/?loja=mercadolivre"
    assert len(transport.images) == 1 and "https://meli.la/abc" in transport.images[0][2]
    again = client.post(url, {"rule": rule.id}, follow=True).content.decode()
    assert "Não saiu:" in again  # o grupo já tem item hoje


def test_send_offer_explains_the_refusal_and_ignores_open_redirects(client, staff, org, rule):
    offer = make_offer(key="MLB777", price=100, frm=200)  # sem link do dono
    response = client.post(reverse("panel:offer_send", args=[offer.id]),
                           {"rule": rule.id, "next": "//evil.io/x"}, follow=True)
    assert "Não saiu: Sem link seu." in response.content.decode()
    assert response.redirect_chain[-1][0] == reverse("panel:offers")
    send = reverse("panel:offer_send", args=[offer.id])
    assert client.post(send, {"rule": 999}).status_code == 404
    assert client.post(reverse("panel:offer_send", args=[999999]),
                       {"rule": rule.id}).status_code == 404


def test_login_page_uses_the_panel_design(client, db):
    html = client.get("/admin/login/").content.decode()
    assert "brand-word" in html and 'name="username"' in html and 'name="password"' in html
    assert "csrfmiddlewaretoken" in html
