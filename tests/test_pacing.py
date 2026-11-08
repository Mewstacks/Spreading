from datetime import timedelta

import pytest
from django.urls import reverse

from apps.delivery.models import OutboxItem
from apps.delivery.outbox import _rule_ready, jittered_interval
from apps.offers.models import Rule
from tests.conftest import NOW

pytestmark = pytest.mark.django_db


def make_rule(org, destination, **over):
    base = dict(org=org, destination=destination, name="Grupo", window_start=8, window_end=20,
                interval_minutes=5, daily_cap=12)
    base.update(over)
    return Rule.unscoped.create(**base)


@pytest.mark.parametrize("start, end, minutes", [
    (8, 20, 720), (0, 23, 1380), (22, 6, 480), (0, 0, 1440), (9, 9, 1440), (23, 0, 60)])
def test_the_window_length_in_minutes(start, end, minutes):
    assert Rule(window_start=start, window_end=end).window_minutes == minutes


def test_a_fixed_rule_keeps_its_interval_and_a_spread_one_fills_the_window(org, destination):
    fixed = make_rule(org, destination)
    assert fixed.pace_minutes == 5
    spread = make_rule(org, destination, name="B", spread=True)
    assert spread.pace_minutes == pytest.approx(54)  # 720 min ÷ 12 envios, com 10% de folga
    slow = make_rule(org, destination, name="C", spread=True, interval_minutes=90)
    assert slow.pace_minutes == 90  # o intervalo escolhido, se maior, vale
    assert make_rule(org, destination, name="D", spread=True, daily_cap=100).pace_minutes == (
        pytest.approx(720 / 30 * 0.9))  # o teto vale o limite de 30
    assert make_rule(org, destination, name="E", spread=True, window_start=0,
                     window_end=0).pace_minutes == pytest.approx(108)


def sent_now(org, destination, rule, key="a"):
    return OutboxItem.unscoped.create(
        org=org, destination=destination, rule=rule, kind="offer", body="x", idempotency_key=key,
        state=OutboxItem.CONFIRMED, next_attempt_at=NOW, created_at=NOW)


def test_the_next_send_of_a_spread_rule_waits_for_its_share_of_the_window(org, destination):
    rule = make_rule(org, destination, spread=True)
    last = sent_now(org, destination, rule)
    gap = jittered_interval(rule, last)
    assert timedelta(minutes=54 * 0.75) <= gap <= timedelta(minutes=54 * 1.25)
    assert not _rule_ready(org, rule, NOW + gap - timedelta(minutes=1))
    assert _rule_ready(org, rule, NOW + gap + timedelta(minutes=1))
    assert _rule_ready(org, rule, NOW + timedelta(minutes=68))  # nunca passa de 54 × 1,25


def test_a_fixed_rule_still_sends_in_a_burst(org, destination):
    rule = make_rule(org, destination)
    sent_now(org, destination, rule)
    assert _rule_ready(org, rule, NOW + timedelta(minutes=7))  # 5 min ± 25%


def test_the_group_page_saves_and_shows_the_pace(client, django_user_model, org, destination,
                                                  monkeypatch):
    monkeypatch.setattr("django.utils.timezone.now", lambda: NOW)
    client.force_login(django_user_model.objects.create_user("dono", password="x", is_staff=True))
    rule = make_rule(org, destination)
    url = reverse("panel:group", args=[rule.id])
    html = client.get(url).content.decode()
    assert 'name="pace"' in html and '<option value="interval" selected>' in html
    client.post(url, {"pace": "spread", "daily_cap": "12", "window_start": "8", "window_end": "20",
                      "interval": "5", "min_discount": "15", "stores": ["shopee"]})
    rule.refresh_from_db()
    assert rule.spread is True
    assert '<option value="spread" selected>' in client.get(url).content.decode()
    client.post(url, {"pace": "qualquer", "daily_cap": "12", "window_start": "8",
                      "window_end": "20", "interval": "5", "min_discount": "15"})
    rule.refresh_from_db()
    assert rule.spread is False  # valor fora da lista volta para intervalo fixo
