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
    assert f"1/{rule.effective_cap} hoje" in html
    assert escape(cat[0][0]) in html and escape(cat[1][0]) in html and "+1" in html
    rule.active = True
    rule.niches = []
    rule.save()
    html = page(client, "groups")
    assert "Ativo" in html and ">Tudo<" in html


def test_groups_page_shows_link_builder_state_only_when_there_is_one(client, staff, org):
    from apps.linkbuilder.models import BuilderState

    assert "Link Builder" not in page(client, "groups")
    state = BuilderState.unscoped.create(
        org=org, state=BuilderState.NO_SESSION, detail="rode tools/ml_login.py", updated_at=NOW)
    html = page(client, "groups")
    assert "Link Builder · Sem sessão" in html and "rode tools/ml_login.py" in html
    assert "tag-warn" in html
    state.state, state.detail = BuilderState.READY, ""
    state.save()
    html = page(client, "groups")
    assert "Link Builder · Pronto" in html and 'tag tag-warn">Link Builder' not in html


# ── adicionar grupo ───────────────────────────────────────────────────────
def test_add_page_lists_free_groups_and_filters(client, staff, org, destination, monkeypatch):
    monkeypatch.setattr("apps.panel.views.WahaTransport", lambda: Groups(GROUPS))
    html = page(client, "add")
    assert "Promo Casa" in html and "Achados Tech" in html
    assert "120 membros" in html and "80 membros" in html
    assert 'class="btn btn-primary"' not in html  # uma ação primária por tela: aqui nenhuma
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
