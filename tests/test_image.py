import io

from PIL import Image

from apps.compose.image import fetch_image_jpeg, safe_image_url, to_jpeg
from apps.delivery.models import OutboxItem
from apps.delivery.outbox import deliver_due
from tests.conftest import NOW, FakeTransport


def png_bytes(size=(800, 600), mode="RGBA", fmt="PNG"):
    image = Image.new(mode, size, (200, 30, 30, 128) if mode == "RGBA" else (200, 30, 30))
    buffer = io.BytesIO()
    image.save(buffer, fmt)
    return buffer.getvalue()


class Resp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status


class Http:
    def __init__(self, resp):
        self.resp, self.urls = resp, []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.resp


def test_safe_image_url_allowlist():
    ok = "https://http2.mlstatic.com/D_NQ_NP_1.webp"
    assert safe_image_url(ok) == ok
    assert safe_image_url("https://m.media-amazon.com/images/I/1.jpg")
    for bad in ["http://http2.mlstatic.com/x.jpg", "https://evil.io/mlstatic.com.jpg",
                "https://169.254.169.254/latest", "file:///etc/passwd", ""]:
        assert safe_image_url(bad) == "", bad


def test_to_jpeg_converts_alpha_and_downscales():
    out = to_jpeg(png_bytes((3000, 2000)))
    image = Image.open(io.BytesIO(out))
    assert image.format == "JPEG" and max(image.size) <= 1600 and len(out) <= 1_500_000


def test_to_jpeg_rejects_tiny_and_garbage():
    assert to_jpeg(png_bytes((100, 100))) is None
    assert to_jpeg(b"nao e imagem") is None


def test_fetch_blocks_ssrf_without_network():
    http = Http(Resp(png_bytes()))
    assert fetch_image_jpeg("https://internal.local/x.png", http=http) is None
    assert http.urls == []
    assert fetch_image_jpeg("https://http2.mlstatic.com/a.png", http=http)
    assert fetch_image_jpeg("https://http2.mlstatic.com/a.png", http=Http(Resp(b"", 404))) is None


class ImgTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.images = []

    def send_image(self, chat_id, jpeg, caption):
        self.images.append((chat_id, len(jpeg), caption))
        from apps.delivery.transports import CONFIRMED, Result

        return Result(True, CONFIRMED, message_id="img1")


def _queued_item(org, destination, image_url, kind="offer"):
    return OutboxItem.unscoped.create(
        org=org, destination=destination, body="legenda", image_url=image_url, kind=kind,
        idempotency_key=f"k{image_url[-5:]}{kind}", next_attempt_at=NOW, created_at=NOW,
    )


def test_deliver_sends_photo_with_caption(org, destination, monkeypatch):
    monkeypatch.setattr("apps.delivery.outbox.fetch_image_jpeg", lambda url: b"jpegbytes")
    item = _queued_item(org, destination, "https://http2.mlstatic.com/a.jpg")
    transport = ImgTransport()
    assert deliver_due(transport, NOW)["confirmado"] == 1
    assert transport.images == [("120363@g.us", 9, "legenda")] and transport.sent == []
    item.refresh_from_db()
    assert item.external_message_id == "img1"


def test_offer_without_usable_photo_is_not_published(org, destination, monkeypatch):
    monkeypatch.setattr("apps.delivery.outbox.fetch_image_jpeg", lambda url: None)
    item = _queued_item(org, destination, "https://http2.mlstatic.com/b.jpg")
    transport = ImgTransport()
    assert deliver_due(transport, NOW)["falhou"] == 1
    assert transport.images == [] and transport.sent == []  # sem foto, sem post
    item.refresh_from_db()
    assert item.last_error == "foto_indisponivel"


def test_coupon_notice_stays_text_only(org, destination):
    _queued_item(org, destination, "", kind="coupon_notice")
    transport = ImgTransport()
    deliver_due(transport, NOW)
    assert transport.sent == [("120363@g.us", "legenda")] and transport.images == []
