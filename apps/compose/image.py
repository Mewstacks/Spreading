"""Foto do produto: busca com guarda de SSRF e entrega em JPEG leve (webp do ML falha no envio)."""
import io
from urllib.parse import urlparse

import requests
from django.conf import settings
from PIL import Image, UnidentifiedImageError

ALLOWED_IMAGE_HOSTS = ("mlstatic.com", "media-amazon.com", "ssl-images-amazon.com")
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
MAX_SIDE = 1600
MAX_OUT_BYTES = 1_500_000
MIN_SIDE = 400


def safe_image_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    ok = parsed.scheme == "https" and any(
        host == h or host.endswith("." + h) for h in ALLOWED_IMAGE_HOSTS
    )
    return url.strip() if ok else ""


def to_jpeg(raw: bytes) -> bytes | None:
    """Converte qualquer imagem em JPEG ≤1600 px e ≤1,5 MB; None se pequena demais ou inválida."""
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return None
    if min(image.size) < MIN_SIDE:
        return None
    if image.mode in ("RGBA", "LA", "P"):
        base = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        base.paste(rgba, mask=rgba.split()[-1])
        image = base
    else:
        image = image.convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    for quality in (88, 80, 70, 60):
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=quality, optimize=True)
        if buffer.tell() <= MAX_OUT_BYTES:
            return buffer.getvalue()
    return None


def fetch_image_jpeg(url: str, http=None) -> bytes | None:
    safe = safe_image_url(url)
    if not safe:
        return None
    try:
        response = (http or requests).get(
            safe, timeout=settings.HTTP_TIMEOUT_SECONDS, stream=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SpreadingBot/2.0)"},
        )
    except requests.exceptions.RequestException:
        return None
    if response.status_code != 200 or len(response.content) > MAX_DOWNLOAD_BYTES:
        return None
    return to_jpeg(response.content)
