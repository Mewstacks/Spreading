import io
import socket
import types

import pytest
from PIL import Image

from apps.compose.image import (
    MAX_DOWNLOAD_BYTES,
    MAX_REDIRECTS,
    fetch_image_jpeg,
    host_is_public,
    safe_image_url,
    to_jpeg,
)
from apps.delivery.models import OutboxItem
from apps.delivery.outbox import deliver_due
from tests.conftest import NOW, FakeTransport

URL = "https://http2.mlstatic.com/a.png"


def png_bytes(size=(800, 600), mode="RGBA", fmt="PNG"):
    image = Image.new(mode, size, (200, 30, 30, 128) if mode == "RGBA" else (200, 30, 30))
    buffer = io.BytesIO()
    image.save(buffer, fmt)
    return buffer.getvalue()


class Resp:
    def __init__(self, content=b"", status=200, headers=None, chunk=64 * 1024):
        self.content, self.status_code = content, status
        self.headers = headers or {}
        self.chunks_read, self.closed, self._chunk = 0, False, chunk

    def iter_content(self, size):
        for start in range(0, len(self.content), self._chunk):
            self.chunks_read += 1
            yield self.content[start:start + self._chunk]

    def close(self):
        self.closed = True


def redirect(location, status=302):
    return Resp(status=status, headers={"Location": location})


class Http:
    """Devolve as respostas em ordem; a última se repete."""

    def __init__(self, *resps):
        self.resps, self.urls, self.kwargs = list(resps), [], []

    def get(self, url, **kw):
        self.urls.append(url)
        self.kwargs.append(kw)
        return self.resps.pop(0) if len(self.resps) > 1 else self.resps[0]


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr("apps.compose.image.host_is_public", lambda host: True)


def test_safe_image_url_allowlist():
    ok = "https://http2.mlstatic.com/D_NQ_NP_1.webp"
    assert safe_image_url(ok) == ok
    assert safe_image_url("https://m.media-amazon.com/images/I/1.jpg")
    for bad in ["http://http2.mlstatic.com/x.jpg", "https://evil.io/mlstatic.com.jpg",
                "https://169.254.169.254/latest", "file:///etc/passwd", ""]:
        assert safe_image_url(bad) == "", bad


def test_host_is_public(monkeypatch):
    def resolves_to(*ips):
        return lambda host, port, proto=0: [(2, 1, 6, "", (ip, port)) for ip in ips]

    cases = [("8.8.8.8", True), ("2606:4700::1111", True), ("10.0.0.5", False),
             ("127.0.0.1", False), ("169.254.169.254", False), ("192.168.1.1", False),
             ("100.64.0.1", False), ("::1", False), ("fe80::1%eth0", False),
             ("::ffff:10.0.0.1", False)]
    for ip, expected in cases:
        monkeypatch.setattr("apps.compose.image.socket.getaddrinfo", resolves_to(ip))
        assert host_is_public("cdn.mlstatic.com") is expected, ip
    monkeypatch.setattr("apps.compose.image.socket.getaddrinfo",
                        resolves_to("8.8.8.8", "10.0.0.5"))
    assert host_is_public("cdn.mlstatic.com") is False  # um endereço privado basta

    def no_dns(*args, **kwargs):
        raise socket.gaierror("sem resolução")

    monkeypatch.setattr("apps.compose.image.socket.getaddrinfo", no_dns)
    assert host_is_public("nope.mlstatic.com") is False


def test_to_jpeg_converts_alpha_and_downscales():
    out = to_jpeg(png_bytes((3000, 2000)))
    image = Image.open(io.BytesIO(out))
    assert image.format == "JPEG" and max(image.size) <= 1600 and len(out) <= 1_500_000


def test_to_jpeg_rejects_tiny_and_garbage():
    assert to_jpeg(png_bytes((100, 100))) is None
    assert to_jpeg(b"nao e imagem") is None


def test_to_jpeg_refuses_huge_canvas_before_decoding():
    canvas = Image.new("1", (7000, 7000))  # 49 Mpx; PNG minúsculo, descomprime enorme
    buffer = io.BytesIO()
    canvas.save(buffer, "PNG")
    assert to_jpeg(buffer.getvalue()) is None


def test_fetch_blocks_ssrf_without_network():
    http = Http(Resp(png_bytes()))
    assert fetch_image_jpeg("https://internal.local/x.png", http=http) is None
    assert http.urls == []
    assert fetch_image_jpeg(URL, http=http)
    assert fetch_image_jpeg(URL, http=Http(Resp(b"", 404))) is None


def test_fetch_refuses_allowlisted_name_that_resolves_private(monkeypatch):
    monkeypatch.setattr("apps.compose.image.host_is_public", lambda host: False)
    http = Http(Resp(png_bytes()))
    assert fetch_image_jpeg(URL, http=http) is None
    assert http.urls == []


def test_fetch_follows_redirect_inside_allowlist_by_hand():
    hop = redirect("/b.png")
    http = Http(hop, Resp(png_bytes()))
    assert fetch_image_jpeg(URL, http=http)
    assert http.urls == [URL, "https://http2.mlstatic.com/b.png"]
    assert all(kw["allow_redirects"] is False and kw["stream"] is True for kw in http.kwargs)
    assert hop.closed


def test_fetch_refuses_redirect_out_of_allowlist():
    targets = ["http://169.254.169.254/latest/meta-data", "https://evil.io/x.png",
               "https://http2.mlstatic.com.evil.io/x.png", "file:///etc/passwd"]
    for target in targets:
        hop = redirect(target)
        http = Http(hop, Resp(png_bytes()))
        assert fetch_image_jpeg(URL, http=http) is None, target
        assert http.urls == [URL], target
        assert hop.closed


def test_fetch_refuses_redirect_to_private_address(monkeypatch):
    monkeypatch.setattr("apps.compose.image.host_is_public",
                        lambda host: host != "rebind.mlstatic.com")
    http = Http(redirect("https://rebind.mlstatic.com/x.png"), Resp(png_bytes()))
    assert fetch_image_jpeg(URL, http=http) is None
    assert http.urls == [URL]


def test_fetch_stops_redirect_loop():
    http = Http(redirect(URL))
    assert fetch_image_jpeg(URL, http=http) is None
    assert len(http.urls) == MAX_REDIRECTS + 1


def test_fetch_refuses_redirect_without_location():
    http = Http(Resp(status=302))
    assert fetch_image_jpeg(URL, http=http) is None
    assert http.urls == [URL]


def test_fetch_rejects_declared_oversize_without_reading():
    big = Resp(png_bytes(), headers={"Content-Length": str(MAX_DOWNLOAD_BYTES + 1)})
    assert fetch_image_jpeg(URL, http=Http(big)) is None
    assert big.chunks_read == 0 and big.closed


def test_fetch_stops_reading_past_cap_when_server_lies(monkeypatch):
    monkeypatch.setattr("apps.compose.image.MAX_DOWNLOAD_BYTES", 200_000)
    body = Resp(b"x" * 1_000_000)  # sem Content-Length
    assert fetch_image_jpeg(URL, http=Http(body)) is None
    assert body.chunks_read <= 4 and body.closed


def test_fetch_gives_up_after_deadline(monkeypatch):
    ticks = iter([0.0, 5.0, 25.0, 30.0])  # prazo nasce em 0 + 20 s; 2º pedaço já passou dele
    monkeypatch.setattr("apps.compose.image.time",
                        types.SimpleNamespace(monotonic=lambda: next(ticks)))
    slow = Resp(png_bytes(), chunk=100)
    assert fetch_image_jpeg(URL, http=Http(slow)) is None
    assert slow.closed


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
