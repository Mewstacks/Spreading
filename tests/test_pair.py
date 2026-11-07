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


def test_qr_image_served_no_cache_or_404(staff_client, monkeypatch):
    fake = FakeWaha()
    monkeypatch.setattr(views, "WahaTransport", lambda **kw: fake)
    response = staff_client.get("/pair/qr.png")
    assert response.status_code == 200 and response["Content-Type"] == "image/png"
    assert "no-store" in response["Cache-Control"]
    fake.image = None
    assert staff_client.get("/pair/qr.png").status_code == 404
