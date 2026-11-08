from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from apps.delivery import checks
from apps.delivery.health import snapshot
from apps.delivery.transports import SessionRouter
from apps.offers.models import Rule, SourceRun
from tests.conftest import NOW, FakeTransport
from tests.test_alerts import rule
from tests.test_sessions import Sessions

pytestmark = pytest.mark.django_db

NOWAIT = {"sleep": lambda seconds: None}


@pytest.fixture(autouse=True)
def no_canary_chat(settings):
    settings.WAHA_SESSION, settings.CANARY_CHAT_ID, settings.CANARY_CHATS = "default", "", ""


def get(label):
    return SourceRun.objects.get(label=label)


def test_every_cycle_probes_each_number_and_records_the_state(org):
    rule(org)
    rule(org, "numero2", "b@g.us")
    router = SessionRouter(Sessions({"default", "numero2"}))
    assert checks.run(router, NOW, **NOWAIT) == {"default": "ok", "numero2": "ok"}
    run = get("wa:default")
    assert (run.ok, run.failures, run.result, run.checked_at, run.result_at) == (
        True, 0, "ok (sondagem)", NOW, NOW)
    assert not SourceRun.objects.filter(label__startswith="wc:").exists()  # sem chat, sem canário


def test_a_silent_or_down_number_counts_failures_and_recovers(org):
    rule(org)
    router = SessionRouter(Sessions({"default"}))
    fake = router.for_session("default")
    fake.alive = False
    assert checks.run(router, NOW, **NOWAIT) == {"default": "falhou"}
    checks.run(router, NOW + timedelta(minutes=5), **NOWAIT)
    run = get("wa:default")
    assert (run.ok, run.failures, run.error) == (False, 2, "o WhatsApp não responde")
    fake.alive, fake.status = True, "FAILED"
    checks.run(router, NOW + timedelta(minutes=10), **NOWAIT)
    run.refresh_from_db()
    assert (run.failures, run.error) == (3, "sessão fora de WORKING")
    fake.status = "WORKING"
    assert checks.run(router, NOW + timedelta(minutes=15), **NOWAIT) == {"default": "ok"}
    run.refresh_from_db()
    assert (run.ok, run.failures, run.error) == (True, 0, "")  # voltou: zera


def test_the_main_number_is_checked_even_with_no_rule_and_a_lone_transport_works(org):
    transport = FakeTransport()
    assert checks.run(transport, NOW, **NOWAIT) == {"default": "ok"}
    assert get("wa:default").ok


def test_only_numbers_in_use_are_checked_besides_the_main_one(org):
    rule(org, "numero2", "b@g.us")
    idle = rule(org, "numero3", "c@g.us")
    Rule.unscoped.filter(id=idle.id).update(active=False)  # sem regra ativa: não está em uso
    router = SessionRouter(Sessions({"default", "numero2", "numero3"}))
    assert set(checks.run(router, NOW, **NOWAIT)) == {"default", "numero2"}


# ── canário: mensagem real, de 2 a 4 horas ────────────────────────────────
@pytest.fixture
def canary(settings):
    settings.CANARY_CHAT_ID = "grupo@g.us"


def acked(router):
    fake = router.for_session("default")
    fake.acks = {f"true_grupo@g.us_{n}": 2 for n in range(1, 9)}
    return fake


def test_the_canary_sends_a_real_message_when_a_chat_is_set(org, canary):
    rule(org)
    router = SessionRouter(Sessions({"default"}))
    fake = acked(router)
    assert checks.run(router, NOW, **NOWAIT) == {"default": "ok+canario"}
    assert fake.sent == [("grupo@g.us", "✔ canário")]
    run = get("wc:default")
    assert (run.ok, run.result, run.checked_at) == (True, "ack 2", NOW)


def test_the_canary_waits_two_to_four_hours_between_messages(org, canary):
    rule(org)
    router = SessionRouter(Sessions({"default"}))
    fake = acked(router)
    checks.run(router, NOW, **NOWAIT)
    checks.run(router, NOW + timedelta(minutes=90), **NOWAIT)
    assert len(fake.sent) == 1  # antes de 2 h nunca
    checks.run(router, NOW + timedelta(hours=4, minutes=1), **NOWAIT)
    assert len(fake.sent) == 2  # com 4 h já venceu, em qualquer sorteio
    gaps = set()
    for seed in range(12):  # o intervalo varia de 2 a 4 h conforme o último envio
        stamp = NOW + timedelta(days=seed)
        SourceRun.objects.filter(label="wc:default").update(checked_at=stamp)
        due = [m for m in range(120, 241, 5)
               if checks.canary_due("default", stamp + timedelta(minutes=m))]
        gaps.add(due[0])
        assert not checks.canary_due("default", stamp + timedelta(minutes=119))
    assert len(gaps) > 3 and min(gaps) >= 120 and max(gaps) <= 240


def test_a_canary_with_no_ack_fails_without_touching_the_probe(org, canary):
    rule(org)
    router = SessionRouter(Sessions({"default"}))  # nenhum ACK: a mensagem não chegou
    assert checks.run(router, NOW, **NOWAIT) == {"default": "ok+canario falhou"}
    run = get("wc:default")
    assert (run.ok, run.failures, run.error) == (False, 1, "sem ACK ≥ 2 no prazo")
    assert get("wa:default").ok


def test_the_canary_is_not_tried_on_a_number_that_is_down(org, canary):
    rule(org)
    router = SessionRouter(Sessions(set()))
    assert checks.run(router, NOW, **NOWAIT) == {"default": "falhou"}
    assert router.for_session("default").sent == []
    assert not SourceRun.objects.filter(label="wc:default").exists()


def test_each_number_uses_its_own_canary_chat(org, settings):
    settings.CANARY_CHATS = "numero2=outro@g.us"
    rule(org)
    rule(org, "numero2", "b@g.us")
    router = SessionRouter(Sessions({"default", "numero2"}))
    router.for_session("numero2").acks = {"true_outro@g.us_1": 2}
    assert checks.run(router, NOW, **NOWAIT) == {"default": "ok", "numero2": "ok+canario"}
    assert router.for_session("numero2").sent == [("outro@g.us", "✔ canário")]
    assert router.for_session("default").sent == []


# ── visibilidade: painel, /status e healthcheck do worker ────────────────
@pytest.fixture
def staff(client, monkeypatch, django_user_model):
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)
    client.force_login(django_user_model.objects.create_user("dono", password="x", is_staff=True))


def test_the_whatsapp_page_shows_the_last_check_of_each_number(client, staff, org, monkeypatch):
    rule(org)
    rule(org, "numero2", "b@g.us")

    class Waha:
        def __init__(self, session_name=None):
            self.session_name = session_name

        def list_sessions(self):
            return []

    monkeypatch.setattr("apps.panel.views.WahaTransport", Waha)
    SourceRun.objects.create(label="wa:default", checked_at=NOW - timedelta(minutes=3), ok=True,
                             result="ok (sondagem)", result_at=NOW - timedelta(minutes=3))
    SourceRun.objects.create(label="wa:numero2", checked_at=NOW, ok=False, failures=3,
                             error="o WhatsApp não responde")
    html = client.get(reverse("panel:whatsapp")).content.decode()
    assert "ok 11:57" in html  # 15:00 UTC = 12:00 em Brasília; a checagem foi 3 min antes
    assert "falhou 3× · o WhatsApp não responde" in html


def test_status_carries_the_checks_and_a_stale_worker_turns_it_red(org, destination):
    from apps.coupons.models import Coupon

    Coupon.objects.create(code="A", source="t", valid_until=NOW.date(), last_seen=NOW)
    SourceRun.objects.create(label="cupons", checked_at=NOW - timedelta(minutes=3), ok=True)
    SourceRun.objects.create(label="wa:default", checked_at=NOW, ok=False, failures=2,
                             error="o WhatsApp não responde")
    data = snapshot(FakeTransport(), NOW)
    assert data["ok"] is True and data["cycle_age_s"] == 0  # o mais recente: a checagem do WhatsApp
    assert data["whatsapp_checks"] == {"default": {"ok": False, "failures": 2,
                                                   "at": NOW.isoformat()}}
    stale = snapshot(FakeTransport(), NOW + timedelta(minutes=30))
    assert stale["cycle_age_s"] == 1800 and stale["ok"] is False
    SourceRun.objects.all().delete()
    assert snapshot(FakeTransport(), NOW)["cycle_age_s"] is None  # nunca rodou: sem nota


def test_the_worker_healthcheck_fails_when_the_cycle_stops():
    out = StringIO()
    with pytest.raises(CommandError, match="nenhum ciclo"):
        call_command("healthcheck", stdout=out)
    SourceRun.objects.create(label="cupons", checked_at=NOW - timedelta(minutes=40), ok=True)
    with pytest.raises(CommandError, match="parado há 40 min"):
        call_command("healthcheck", now=NOW, stdout=out)
    SourceRun.objects.filter(label="cupons").update(checked_at=NOW - timedelta(minutes=4))
    call_command("healthcheck", now=NOW, stdout=out)
    assert "ciclo há 4 min" in out.getvalue()
