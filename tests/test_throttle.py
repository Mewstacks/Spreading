from datetime import timedelta

import pytest
from django.utils import timezone

from apps.orgs.models import LoginFailure

pytestmark = pytest.mark.django_db

LOGIN = "/admin/login/"


@pytest.fixture
def owner(django_user_model):
    return django_user_model.objects.create_user("dono", password="senha-certa-123", is_staff=True)


def attempt(client, password="errada", user="dono", **extra):
    return client.post(LOGIN, {"username": user, "password": password, "next": "/painel/"}, **extra)


def test_the_fifth_wrong_password_closes_the_door_even_for_the_right_one(client, owner):
    for _ in range(5):
        assert attempt(client).status_code == 200  # a página de login de novo
    blocked = attempt(client, "senha-certa-123")
    assert blocked.status_code == 429 and blocked["Retry-After"] == "900"
    assert "Muitas tentativas" in blocked.content.decode()
    assert LoginFailure.objects.count() == 5  # a tentativa barrada não conta nem passa pelo login
    assert client.get("/painel/").status_code == 302  # e não houve sessão


def test_it_opens_again_after_the_window(client, owner):
    for _ in range(5):
        attempt(client)
    assert attempt(client, "senha-certa-123").status_code == 429
    LoginFailure.objects.update(at=timezone.now() - timedelta(minutes=16))
    assert attempt(client, "senha-certa-123").status_code == 302  # entrou


def test_a_good_login_wipes_the_counter(client, owner):
    for _ in range(4):
        attempt(client)
    assert attempt(client, "senha-certa-123").status_code == 302
    assert not LoginFailure.objects.exists()
    client.logout()
    for _ in range(4):  # recomeça do zero: ainda cabem quatro erros
        assert attempt(client).status_code == 200
    assert attempt(client, "senha-certa-123").status_code == 302


def test_each_address_has_its_own_count(client, owner):
    for _ in range(5):
        attempt(client, REMOTE_ADDR="203.0.113.7")
    assert attempt(client, "senha-certa-123", REMOTE_ADDR="203.0.113.7").status_code == 429
    assert attempt(client, "senha-certa-123", REMOTE_ADDR="198.51.100.9").status_code == 302


def test_only_the_login_form_is_throttled(client, owner):
    for _ in range(5):
        attempt(client)
    assert client.get(LOGIN).status_code == 200  # ver a página nunca é barrado
    assert client.get("/healthz").status_code == 200
    assert client.post("/painel/manual/", {}).status_code == 302  # outra rota segue o fluxo normal


def test_behind_tls_the_address_is_the_one_the_proxy_appended(client, owner, settings):
    settings.HTTPS = True
    forged = {"HTTP_X_FORWARDED_FOR": "1.1.1.1, 203.0.113.7", "REMOTE_ADDR": "10.0.0.2"}
    for _ in range(5):
        attempt(client, secure=True, **forged)
    assert set(LoginFailure.objects.values_list("ip", flat=True)) == {"203.0.113.7"}
    # trocar o primeiro item do cabeçalho não escapa da conta do endereço real
    other = {"HTTP_X_FORWARDED_FOR": "9.9.9.9, 203.0.113.7", "REMOTE_ADDR": "10.0.0.2"}
    assert attempt(client, "senha-certa-123", secure=True, **other).status_code == 429


def test_without_a_proxy_the_forwarded_header_is_not_trusted(client, owner, settings):
    settings.HTTPS = False
    for number in range(5):
        attempt(client, HTTP_X_FORWARDED_FOR=f"1.1.1.{number}", REMOTE_ADDR="203.0.113.7")
    assert set(LoginFailure.objects.values_list("ip", flat=True)) == {"203.0.113.7"}


def test_old_failures_are_cleaned_as_new_ones_arrive(client, owner):
    LoginFailure.objects.create(ip="1.2.3.4", username="x", at=timezone.now() - timedelta(days=2))
    attempt(client)
    assert LoginFailure.objects.count() == 1
    assert LoginFailure.objects.get().username == "dono"


def test_the_limits_come_from_the_settings(client, owner, settings):
    settings.LOGIN_MAX_FAILURES = 2
    attempt(client)
    attempt(client)
    assert attempt(client, "senha-certa-123").status_code == 429
