from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.urls import reverse

import apps.delivery.management.commands.run_cycle as cycle
from apps.delivery.health import snapshot
from apps.delivery.models import OutboxItem
from apps.delivery.outbox import _destination_ready, deliver_due
from apps.delivery.transports import SessionRouter, WahaTransport
from apps.offers.models import Rule
from apps.orgs.models import Destination
from tests.conftest import NOW, FakeTransport
from tests.test_panel import Groups, frozen, staff  # noqa: F401  (fixtures)


class Sessions:
    """Fábrica de transportes falsos: um por número, `working` diz quais estão conectados."""

    def __init__(self, working=()):
        self.made, self.working = {}, set(working)

    def __call__(self, session_name):
        fake = FakeTransport()
        fake.status = "WORKING" if session_name in self.working else "FAILED"
        self.made[session_name] = fake
        return fake


def group(org, chat, session="default", name=None):
    return Destination.unscoped.create(
        org=org, kind=Destination.WA_GROUP, external_id=chat, name=name or chat, session=session)


def queue(org, destination, key):
    return OutboxItem.unscoped.create(
        org=org, destination=destination, body=f"msg {key}", idempotency_key=key,
        next_attempt_at=NOW, created_at=NOW)


def test_router_keeps_one_transport_per_number():
    made = Sessions({"default"})
    router = SessionRouter(made)
    assert router.for_session("default") is router.for_session("default")
    assert router.for_session("numero2") is not router.for_session("default")
    assert sorted(made.made) == ["default", "numero2"]


def test_each_group_is_sent_by_its_own_number(org):
    first, second = group(org, "a@g.us"), group(org, "b@g.us", "numero2")
    queue(org, first, "k1")
    queue(org, second, "k2")
    made = Sessions({"default", "numero2"})
    assert deliver_due(SessionRouter(made), NOW)["confirmado"] == 2
    assert made.made["default"].sent == [("a@g.us", "msg k1")]
    assert made.made["numero2"].sent == [("b@g.us", "msg k2")]


def test_a_number_that_is_down_keeps_its_queue(org):
    first, second = group(org, "a@g.us"), group(org, "b@g.us", "numero2")
    queue(org, first, "k1")
    queue(org, second, "k2")
    made = Sessions({"default"})
    assert deliver_due(SessionRouter(made), NOW, sessions={"default"})["confirmado"] == 1
    assert OutboxItem.unscoped.get(idempotency_key="k2").state == OutboxItem.QUEUED
    assert "numero2" not in made.made  # nem chegou a falar com o número fora do ar


def test_the_cycle_skips_rules_of_a_number_that_is_down(org, monkeypatch):
    first, second = group(org, "a@g.us"), group(org, "b@g.us", "numero2")
    for dest in (first, second):
        Rule.unscoped.create(org=org, destination=dest, name=dest.name)
    seen = []
    monkeypatch.setattr(cycle, "enqueue_for_rule", lambda rule, now: seen.append(rule.name))
    router = SessionRouter(Sessions({"default"}))
    report = cycle.run_once(router, NOW, coupon_html="", offers_html="")
    assert seen == ["a@g.us"] and report["entrega"]["confirmado"] == 0
    down = cycle.run_once(SessionRouter(Sessions()), NOW, coupon_html="", offers_html="")
    assert "nenhum número" in down["entrega"] and seen == ["a@g.us"]


def test_a_number_has_an_hourly_cap_shared_by_its_groups(org, settings):
    settings.SESSION_HOURLY_CAP = 2
    first, second = group(org, "a@g.us"), group(org, "b@g.us")
    other = group(org, "c@g.us", "numero2")
    for n, dest in enumerate((first, second)):
        OutboxItem.unscoped.create(
            org=org, destination=dest, body="x", idempotency_key=f"c{n}",
            state=OutboxItem.CONFIRMED, next_attempt_at=NOW, created_at=NOW - timedelta(minutes=20))
    assert not _destination_ready(org, first, NOW) and not _destination_ready(org, second, NOW)
    assert _destination_ready(org, other, NOW)  # outro número, outro teto
    assert _destination_ready(org, first, NOW + timedelta(minutes=45))  # a hora passou
    settings.SESSION_HOURLY_CAP = 0
    assert _destination_ready(org, first, NOW)


def test_pair_takes_a_valid_session_name_and_ignores_the_rest(client, staff, monkeypatch):  # noqa: F811
    asked = []

    def fake(session_name=None):
        asked.append(session_name)
        return SimpleNamespace(ensure_started=lambda: "SCAN_QR_CODE", qr_png=lambda: b"png")

    monkeypatch.setattr("apps.delivery.views.WahaTransport", fake)
    page = client.get("/pair?session=numero2").content.decode()
    assert "/pair/qr.png?session=numero2" in page
    client.get("/pair?session=../x%22%3E")  # nome fora do padrão cai na sessão principal
    assert client.get("/pair/qr.png?session=numero2").status_code == 200
    assert asked == ["numero2", "default", "numero2"]


def test_a_group_is_added_to_the_chosen_number(client, staff, org, monkeypatch):  # noqa: F811
    asked = []

    def fake(session_name=None):
        asked.append(session_name)
        return Groups([{"id": "777@g.us", "name": "Grupo do 2", "size": 9}])

    monkeypatch.setattr("apps.panel.views.WahaTransport", fake)
    html = client.get(reverse("panel:add"), {"sessao": "numero2"}).content.decode()
    assert "Grupo do 2" in html and 'name="sessao" value="numero2"' in html
    response = client.post(reverse("panel:add"), {"group": "777@g.us", "sessao": "numero2"})
    assert response.status_code == 302
    dest = Destination.objects.for_org(org).get(external_id="777@g.us")
    assert dest.session == "numero2" and set(asked) == {"numero2"}


def test_whatsapp_page_lists_numbers_and_adds_one(client, staff, org, monkeypatch):  # noqa: F811
    group(org, "a@g.us")
    group(org, "b@g.us")
    group(org, "c@g.us", "numero2")
    started = []

    class Fake:
        def __init__(self, session_name=None):
            self.session_name = session_name

        def list_sessions(self):
            return [{"name": "default", "status": "WORKING"},
                    {"name": "numero2", "status": "SCAN_QR_CODE"}]

        def ensure_started(self):
            started.append(self.session_name)
            return "STARTING"

    monkeypatch.setattr("apps.panel.views.WahaTransport", Fake)
    html = client.get(reverse("panel:whatsapp")).content.decode()
    assert "2 números" in html and "Conectado" in html and "Ler o QR" in html
    assert "2 grupos" in html and "1 grupo<" in html and "/pair?session=numero2" in html
    response = client.post(reverse("panel:whatsapp"), {"name": "numero3"})
    assert response.status_code == 302 and response["Location"] == "/pair?session=numero3"
    assert started == ["numero3"]
    bad = client.post(reverse("panel:whatsapp"), {"name": "Nome Ruim!"})
    assert bad["Location"] == reverse("panel:whatsapp") and started == ["numero3"]


def test_five_numbers_is_the_ceiling(client, staff, org, monkeypatch):  # noqa: F811
    started = []

    class Fake:
        def __init__(self, session_name=None):
            self.session_name = session_name

        def list_sessions(self):
            return [{"name": f"n{n}", "status": "WORKING"} for n in range(5)]

        def ensure_started(self):
            started.append(self.session_name)

    monkeypatch.setattr("apps.panel.views.WahaTransport", Fake)
    html = client.get(reverse("panel:whatsapp")).content.decode()
    assert "Novo número" not in html  # sem formulário quando já são 5
    client.post(reverse("panel:whatsapp"), {"name": "n9"})
    assert started == []  # nem o POST direto cria o sexto
    client.post(reverse("panel:whatsapp"), {"name": "n3"})  # reconectar um existente pode
    assert started == ["n3"]


def test_the_groups_badge_summarizes_all_numbers(client, staff, org, monkeypatch):  # noqa: F811
    group(org, "a@g.us")
    group(org, "c@g.us", "numero2")
    states = {"default": "WORKING", "numero2": "FAILED"}

    class Fake:
        def __init__(self, session_name=None):
            self.session_name = session_name

        def session_status(self, timeout=None):
            return states[self.session_name]

    monkeypatch.setattr("apps.panel.views.WahaTransport", Fake)
    assert "1 de 2 fora do ar" in client.get(reverse("panel:groups")).content.decode()
    states["numero2"] = "WORKING"
    assert "2 números" in client.get(reverse("panel:groups")).content.decode()


def test_health_reports_every_number_in_use(org):
    Rule.unscoped.create(org=org, destination=group(org, "a@g.us"), name="A")
    Rule.unscoped.create(org=org, destination=group(org, "c@g.us", "numero2"), name="C")
    router = SessionRouter(Sessions({"default"}))
    data = snapshot(router, NOW)
    assert data["whatsapp_sessions"] == {"default": "WORKING", "numero2": "FAILED"}
    assert data["whatsapp_session"] == "FAILED" and not data["ok"]
    assert snapshot(SessionRouter(Sessions({"default", "numero2"})), NOW)[
        "whatsapp_session"] == "WORKING"


def test_list_sessions_reads_the_waha_listing():
    class Http:
        def get(self, url, params=None, headers=None, timeout=None):
            assert url.endswith("/api/sessions") and params == {"all": "true"}
            return SimpleNamespace(status_code=200, json=lambda: [
                {"name": "default", "status": "WORKING"}, {"name": "n2"}, {"nome": "sem"}, "lixo"])

    rows = WahaTransport(http=Http(), base_url="http://x", api_key="k").list_sessions()
    assert rows == [{"name": "default", "status": "WORKING"}, {"name": "n2", "status": ""}]


@pytest.mark.parametrize("name,valid", [("numero2", True), ("a", True), ("A", False), ("-x", False),
                                        ("a b", False), ("x" * 33, False), ("../etc", False)])
def test_session_names_are_restricted(name, valid):
    from apps.delivery.transports import SESSION_NAME

    assert bool(SESSION_NAME.match(name)) is valid
