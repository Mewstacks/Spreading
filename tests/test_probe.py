from datetime import timedelta
from io import StringIO
from types import SimpleNamespace

import pytest
import requests
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.delivery import alerts
from apps.delivery.health import canary_chats, run_probe
from apps.delivery.transports import SessionRouter, WahaTransport
from tests.conftest import NOW, FakeTransport
from tests.test_alerts import rule
from tests.test_sessions import Sessions

ME = "555496202127@c.us"


@pytest.fixture
def on(settings):
    settings.ALERT_CHAT_ID, settings.ALERT_EMAIL_TO = "5511@c.us", ""


class Http:
    """Fala com o WAHA: a sessão diz quem é o número e `check-exists` pergunta ao WhatsApp."""

    def __init__(self, me=ME, exists=True, status=200, error=None):
        self.me, self.exists, self.status, self.error, self.calls = me, exists, status, error, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if self.error:
            raise self.error
        if url.endswith("/api/sessions/default"):
            body = {"me": {"id": self.me}} if self.me is not None else {"status": "STARTING"}
            return SimpleNamespace(status_code=200, json=lambda: body)
        return SimpleNamespace(status_code=self.status,
                               json=lambda: {"numberExists": self.exists})


def transport(http):
    return WahaTransport(base_url="http://waha:3000", api_key="chave", session_name="default",
                         http=http)


def test_reachable_asks_the_whatsapp_server_about_the_own_number():
    http = Http()
    assert transport(http).reachable() is True
    (me_url, _p, _h), (probe_url, params, headers) = http.calls
    assert me_url == "http://waha:3000/api/sessions/default"
    assert probe_url == "http://waha:3000/api/contacts/check-exists"
    assert params == {"phone": "555496202127", "session": "default"}  # só dígitos, sem @c.us
    assert headers == {"X-Api-Key": "chave"}


@pytest.mark.parametrize("http", [
    Http(exists=False),
    Http(status=500),
    Http(status=422),
    Http(me=None),
    Http(me="sem-numero@c.us"),
    Http(error=requests.exceptions.ConnectTimeout()),
])
def test_reachable_is_false_whenever_the_server_does_not_confirm(http):
    assert transport(http).reachable() is False


def test_a_garbled_answer_is_not_a_confirmation():
    http = Http()
    http.get = lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: (_ for _ in ()).throw(
        ValueError("não é json")))
    assert transport(http).reachable() is False


def test_the_probe_needs_a_working_session_and_a_server_that_answers():
    fake = FakeTransport()
    assert run_probe(fake) == {"ok": True, "probe": True}
    fake.alive = False
    assert run_probe(fake) == {"ok": False, "reason": "o WhatsApp não responde"}
    fake.status = "FAILED"
    assert run_probe(fake) == {"ok": False, "reason": "sessão fora de WORKING"}


def test_a_transport_without_the_probe_counts_as_answering():
    bare = SimpleNamespace(session_working=lambda: True)
    assert run_probe(bare)["ok"] is True


# ── alertas ───────────────────────────────────────────────────────────────
def test_a_connected_number_that_does_not_answer_is_reported_after_the_grace(org, on):
    rule(org)
    made = Sessions({"default"})
    router = SessionRouter(made)
    router.for_session("default").alive = False
    assert alerts.run(router, NOW) == {"ativos": 1, "enviados": 0}  # carência de 10 min
    assert alerts.run(router, NOW + timedelta(minutes=11))["enviados"] == 0  # só ele, e está mudo
    assert "conectado, mas o WhatsApp não responde" in alerts.conditions(NOW, router)["wa:default"]


def test_the_silent_number_is_reported_by_a_live_one_and_recovery_is_announced(org, on):
    rule(org)
    rule(org, "numero2", "b@g.us")
    made = Sessions({"default", "numero2"})
    router = SessionRouter(made)
    router.for_session("numero2").alive = False
    alerts.run(router, NOW)
    assert alerts.run(router, NOW + timedelta(minutes=11)) == {"ativos": 1, "enviados": 1}
    first = router.for_session("default").sent
    assert "Número numero2 conectado, mas o WhatsApp não responde" in first[0][1]
    router.for_session("numero2").alive = True
    alerts.run(router, NOW + timedelta(minutes=20))
    assert "voltou ao normal (wa:numero2)" in router.for_session("default").sent[-1][1]


def test_a_number_that_is_down_is_not_probed(org, on):
    rule(org)
    router = SessionRouter(Sessions(set()))  # tudo fora do ar
    router.for_session("default").alive = False
    assert alerts.conditions(NOW, router)["wa:default"].startswith("Número default fora do ar")


# ── canário por número ────────────────────────────────────────────────────
def test_canary_chats_come_from_the_settings(settings):
    settings.WAHA_SESSION, settings.CANARY_CHAT_ID = "default", "grupo@g.us"
    settings.CANARY_CHATS = "numero2=outro@g.us, ruim, =x ,numero3="
    assert canary_chats() == {"numero2": "outro@g.us", "default": "grupo@g.us"}
    settings.CANARY_CHATS = "default=explicito@g.us"
    assert canary_chats()["default"] == "explicito@g.us"  # o mapa vence o CANARY_CHAT_ID


def run_command(monkeypatch, router):
    monkeypatch.setattr("apps.delivery.management.commands.canary.SessionRouter", lambda: router)
    out = StringIO()
    try:
        call_command("canary", stdout=out)
    except CommandError as exc:
        return out.getvalue(), str(exc)
    return out.getvalue(), ""


def test_the_canary_command_checks_every_number_in_use(org, settings, monkeypatch, db):
    settings.WAHA_SESSION, settings.CANARY_CHAT_ID, settings.CANARY_CHATS = (
        "default", "grupo@g.us", "")
    rule(org)
    rule(org, "numero2", "b@g.us")
    router = SessionRouter(Sessions({"default", "numero2"}))
    router.for_session("default").acks = {"true_grupo@g.us_1": 2}  # o aparelho recebeu
    out, error = run_command(monkeypatch, router)
    assert error == ""
    assert "default: ok ack=2" in out  # tem chat: mensagem real e ACK
    assert "numero2: ok (sondagem)" in out  # sem chat: pergunta ao WhatsApp
    assert router.for_session("default").sent[0][0] == "grupo@g.us"
    assert router.for_session("numero2").sent == []  # nunca manda mensagem sem chat de canário


def test_the_canary_command_fails_when_any_number_fails(org, settings, monkeypatch, db):
    settings.WAHA_SESSION, settings.CANARY_CHAT_ID, settings.CANARY_CHATS = "default", "", ""
    rule(org)
    rule(org, "numero2", "b@g.us")
    router = SessionRouter(Sessions({"default", "numero2"}))
    router.for_session("numero2").alive = False
    out, error = run_command(monkeypatch, router)
    assert "default: ok (sondagem)" in out
    assert "numero2: FALHOU o WhatsApp não responde" in out
    assert error == "canário falhou"


def test_the_canary_command_covers_the_main_number_even_with_no_rules(settings, monkeypatch, db):
    settings.WAHA_SESSION, settings.CANARY_CHAT_ID, settings.CANARY_CHATS = "default", "", ""
    out, error = run_command(monkeypatch, SessionRouter(Sessions({"default"})))
    assert "default: ok (sondagem)" in out and error == ""
