import pytest
from django.contrib.auth import get_user_model

from apps.delivery import views


class FakeWaha:
    status = "SCAN_QR_CODE"
    image = b"\x89PNGfake"

    def ensure_started(self):
        return self.status

    def qr_png(self):
        return self.image


@pytest.fixture
def staff_client(client, db):
    user = get_user_model().objects.create_user("adm", password="x", is_staff=True)
    client.force_login(user)
    return client


def test_pair_requires_staff(client, db):
    assert client.get("/pair").status_code == 302
    assert client.get("/pair/qr.png").status_code == 302


def test_pair_shows_qr_then_connected(staff_client, monkeypatch):
    fake = FakeWaha()
    monkeypatch.setattr(views, "WahaTransport", lambda **kw: fake)
    page = staff_client.get("/pair").content.decode()
    assert "/pair/qr.png" in page and 'http-equiv="refresh"' in page
    fake.status = "WORKING"
    page = staff_client.get("/pair").content.decode()
    assert "Conectado" in page and "qr.png" not in page


@pytest.mark.parametrize("status", ["PASSKEY_REQUIRED", "PASSKEY_CONFIRMATION_REQUIRED"])
def test_passkey_sends_the_owner_to_the_waha_dashboard(staff_client, monkeypatch, settings, status):
    fake = FakeWaha()
    fake.status = status
    settings.WAHA_DASHBOARD_URL = "http://localhost:3000/dashboard/"
    monkeypatch.setattr(views, "WahaTransport", lambda **kw: fake)
    page = staff_client.get("/pair").content.decode()
    assert "Passkey" in page and "Abrir WAHA" in page
    assert 'href="http://localhost:3000/dashboard/"' in page and 'rel="noopener"' in page
    assert "qr.png" not in page and 'http-equiv="refresh"' in page


def test_an_unknown_status_is_shown_escaped(staff_client, monkeypatch):
    fake = FakeWaha()
    fake.status = "<b>x</b>"
    monkeypatch.setattr(views, "WahaTransport", lambda **kw: fake)
    page = staff_client.get("/pair").content.decode()
    assert "&lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page


def test_qr_image_served_no_cache_or_404(staff_client, monkeypatch):
    fake = FakeWaha()
    monkeypatch.setattr(views, "WahaTransport", lambda **kw: fake)
    response = staff_client.get("/pair/qr.png")
    assert response.status_code == 200 and response["Content-Type"] == "image/png"
    assert "no-store" in response["Cache-Control"]
    fake.image = None
    assert staff_client.get("/pair/qr.png").status_code == 404
