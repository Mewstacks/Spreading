from datetime import timedelta

import pytest
from django.core import mail

from apps.delivery import alerts
from apps.delivery.models import Alert, OutboxItem
from apps.delivery.transports import SessionRouter
from apps.offers.models import Rule, SourceRun
from apps.orgs.models import Destination
from tests.conftest import NOW
from tests.test_sessions import Sessions


@pytest.fixture
def on(settings):
    settings.ALERT_CHAT_ID = "5511@c.us"
    settings.ALERT_EMAIL_TO = ""


def router(*working):
    return SessionRouter(Sessions(set(working)))


def rule(org, session="default", chat="a@g.us"):
    dest = Destination.unscoped.create(org=org, kind=Destination.WA_GROUP, external_id=chat,
                                       name=chat, session=session)
    return Rule.unscoped.create(org=org, destination=dest, name=chat)


def sent(transport, session="default"):
    return transport.for_session(session).sent


def test_nothing_is_sent_unless_configured(org, settings):
    settings.ALERT_CHAT_ID = settings.ALERT_EMAIL_TO = ""
    SourceRun.objects.create(label="amazon", checked_at=NOW, ok=False, failures=9, error="x")
    transport = router("default")
    assert alerts.run(transport, NOW) == {} and not Alert.objects.exists()


def test_a_failing_source_warns_once_and_repeats_every_six_hours(org, on):
    rule(org)
    SourceRun.objects.create(label="amazon", checked_at=NOW, ok=False, failures=3,
                             error="captcha")
    transport = router("default")
    assert alerts.run(transport, NOW) == {"ativos": 1, "enviados": 1}
    assert sent(transport) == [("5511@c.us", "⚠️ Spreading: Fonte amazon falhando 3×: captcha")]
    assert alerts.run(transport, NOW + timedelta(hours=1))["enviados"] == 0
    assert alerts.run(transport, NOW + timedelta(hours=7))["enviados"] == 1
    assert len(sent(transport)) == 2


def test_two_failures_are_not_an_incident_and_recovery_is_announced(org, on):
    rule(org)
    run = SourceRun.objects.create(label="amazon", checked_at=NOW, ok=False, failures=2)
    transport = router("default")
    assert alerts.run(transport, NOW)["ativos"] == 0
    run.failures = 3
    run.save()
    alerts.run(transport, NOW + timedelta(minutes=5))
    run.failures = 0
    run.save()
    assert alerts.run(transport, NOW + timedelta(minutes=10)) == {"ativos": 0, "enviados": 1}
    assert sent(transport)[-1][1] == "⚠️ Spreading: voltou ao normal (fonte:amazon)."
    assert not Alert.objects.exists()


def test_a_number_down_waits_ten_minutes_and_is_reported_by_a_live_number(org, on):
    rule(org)
    rule(org, "numero2", "b@g.us")
    transport = router("default")  # numero2 fora do ar
    assert alerts.run(transport, NOW) == {"ativos": 1, "enviados": 0}  # carência
    assert alerts.run(transport, NOW + timedelta(minutes=11)) == {"ativos": 1, "enviados": 1}
    assert "Número numero2 fora do ar" in sent(transport)[0][1]  # saiu pelo número vivo
    assert transport.for_session("numero2").sent == []


def test_every_number_down_leaves_only_the_email(org, on, settings):
    settings.ALERT_EMAIL_TO = "dono@exemplo.com, outro@exemplo.com"
    rule(org)
    transport = router()
    alerts.run(transport, NOW)
    alerts.run(transport, NOW + timedelta(minutes=11))
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["dono@exemplo.com", "outro@exemplo.com"]
    assert "Número default fora do ar" in mail.outbox[0].body
    assert sent(transport) == []


def test_uncertain_and_stuck_items_warn(org, on):
    dest = rule(org).destination
    OutboxItem.unscoped.create(org=org, destination=dest, body="x", idempotency_key="u1",
                               state=OutboxItem.UNCERTAIN, next_attempt_at=NOW, created_at=NOW)
    other = rule(org, chat="c@g.us").destination
    OutboxItem.unscoped.create(org=org, destination=other, body="y", idempotency_key="q1",
                               next_attempt_at=NOW - timedelta(minutes=40), created_at=NOW)
    transport = router("default")
    assert alerts.run(transport, NOW) == {"ativos": 2, "enviados": 2}
    texts = " | ".join(text for _chat, text in sent(transport))
    assert "1 envio(s) incerto(s)" in texts and "1 mensagem(ns) parada(s)" in texts


def test_a_broken_channel_never_breaks_the_cycle(org, on, settings, monkeypatch):
    settings.ALERT_EMAIL_TO = "dono@exemplo.com"
    rule(org)
    SourceRun.objects.create(label="amazon", checked_at=NOW, ok=False, failures=3)

    def boom(*args, **kwargs):
        raise OSError("smtp fora")

    monkeypatch.setattr(alerts, "send_mail", boom)
    assert alerts.run(router("default"), NOW)["enviados"] == 1  # o WhatsApp entregou
