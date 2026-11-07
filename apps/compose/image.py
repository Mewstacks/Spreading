"""Foto do produto: busca com guarda de SSRF e entrega em JPEG leve (webp do ML falha no envio).

Defesas na busca, nesta ordem:
- só https em CDN de imagem da lista (`safe_image_url`);
- o host precisa resolver para endereço público (nada de loopback, privado, link-local);
- redirecionamento é seguido à mão, no máximo `MAX_REDIRECTS` saltos, e cada destino passa de
  novo pelas duas checagens acima (um 302 para o metadata da nuvem não atravessa);
- o corpo é lido em pedaços e a leitura para ao passar de `MAX_DOWNLOAD_BYTES` ou do prazo total,
  então um servidor lento ou gigante não segura o worker nem a memória.
Resta a janela entre a resolução do DNS e a conexão; como o host é CDN da lista, o risco é mínimo.
"""
import io
import ipaddress
import socket
import time
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings
from PIL import Image, UnidentifiedImageError

ALLOWED_IMAGE_HOSTS = ("mlstatic.com", "media-amazon.com", "ssl-images-amazon.com",
                       "susercontent.com", "shopee.com.br")
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 3
CHUNK_BYTES = 64 * 1024
DOWNLOAD_DEADLINE_SECONDS = 20
MAX_PIXELS = 40_000_000
MAX_SIDE = 1600
MAX_OUT_BYTES = 1_500_000
MIN_SIDE = 400
REDIRECT_CODES = (301, 302, 303, 307, 308)
USER_AGENT = {"User-Agent": "Mozilla/5.0 (compatible; SpreadingBot/2.0)"}


def safe_image_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    ok = parsed.scheme == "https" and any(
        host == h or host.endswith("." + h) for h in ALLOWED_IMAGE_HOSTS
    )
    return url.strip() if ok else ""


def host_is_public(host: str) -> bool:
    """True só se todo endereço do host é público. Falha de DNS conta como não."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except ValueError:
            return False
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global:
            return False
    return True


def to_jpeg(raw: bytes) -> bytes | None:
    """Converte qualquer imagem em JPEG ≤1600 px e ≤1,5 MB; None se pequena demais ou inválida."""
    try:
        image = Image.open(io.BytesIO(raw))
        if image.size[0] * image.size[1] > MAX_PIXELS:
            return None
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


def _read_capped(response, deadline: float) -> bytes | None:
    declared = response.headers.get("Content-Length", "")
    if declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
        return None
    body = bytearray()
    for chunk in response.iter_content(CHUNK_BYTES):
        body.extend(chunk)
        if len(body) > MAX_DOWNLOAD_BYTES or time.monotonic() > deadline:
            return None
    return bytes(body)


def _download(url: str, http, deadline: float) -> bytes | None:
    client = http or requests
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        safe = safe_image_url(current)
        if not safe or not host_is_public(urlparse(safe).hostname or ""):
            return None
        response = client.get(
            safe, timeout=settings.HTTP_TIMEOUT_SECONDS, stream=True,
            allow_redirects=False, headers=USER_AGENT,
        )
        try:
            if response.status_code in REDIRECT_CODES:
                location = response.headers.get("Location", "")
                if not location:
                    return None
                current = urljoin(safe, location)
                continue
            if response.status_code != 200:
                return None
            return _read_capped(response, deadline)
        finally:
            response.close()
    return None


def fetch_image_jpeg(url: str, http=None) -> bytes | None:
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
    try:
        raw = _download(url, http, deadline)
    except requests.exceptions.RequestException:
        return None
    return to_jpeg(raw) if raw else None
