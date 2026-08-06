"""Colagem de fotos de produtos numa imagem única, p/ a mensagem de cupom.

A mensagem de cupom (ver `ofertas.montar_mensagem_cupom_produtos`) mostra vários
produtos de uma vez; o WhatsApp só manda UMA imagem por mensagem, então as fotos
dos produtos são reduzidas e coladas numa grade sobre fundo branco — a cliente vê
tudo de relance, no formato pedido.

Depende só de Pillow (já usado em `ofertas._baixar_imagem_b64`).
"""
import base64
import ipaddress
import logging
import math
import socket
from io import BytesIO
from urllib.parse import urljoin, urlsplit

import requests

logger = logging.getLogger(__name__)

_TELA = 1080          # lado da imagem final (quadrada, padrão de card)
_MARGEM = 12          # respiro branco entre as células
_FUNDO = (255, 255, 255)
_MAX_BYTES = 8 * 1024 * 1024   # teto do DOWNLOAD de cada foto de origem
_LIMIAR_FUNDO_BRANCO = 12
_RESPIRO_PRODUTO = 0.06

# Teto do que vai para o transporte. O worker do WhatsApp tem um orçamento único
# (SEND_REQUEST_TIMEOUT_MS, hoje 55s) para preflight MAIS upload da mídia; se o
# sendMessage começa e estoura esse prazo, ele devolve `resultado: "incerto"` e a
# oferta não é reenviada — para não duplicar no grupo. Ou seja: uma foto grande
# demais não vira erro de imagem, vira uma entrega que ninguém sabe se aconteceu.
# Ver node.js/index.js (timeoutComEnvioIniciado) e ofertas._motivo_publico_transporte.
#
# A colagem já saía dentro do teto por construção (1080x1080). O caminho de foto
# única do produto não: mandava a imagem na resolução original da loja.
LADO_MAXIMO_ENVIO = 1600
MAX_BYTES_ENVIO = 1_500_000    # antes do base64, que ainda infla ~33%
_QUALIDADES_JPEG = (85, 75, 65, 55)


def _url_publica(url):
    """Rejeita esquemas/hosts locais e IPs não públicos antes de qualquer GET."""
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.casefold().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return bool(infos)


def _baixar_imagem(url):
    """URL -> PIL.Image RGB, ou None. Mesma conversão de _baixar_imagem_b64 (webp falha)."""
    if not _url_publica(url):
        return None
    try:
        from PIL import Image
        atual = str(url)
        for _ in range(4):
            r = requests.get(atual, timeout=8, stream=True, allow_redirects=False,
                             headers={"Accept": "image/*"})
            if r.status_code in (301, 302, 303, 307, 308):
                destino = urljoin(atual, r.headers.get("Location") or "")
                r.close()
                if not _url_publica(destino):
                    return None
                atual = destino
                continue
            if r.status_code != 200:
                r.close()
                return None
            content_type = str(r.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("image/"):
                r.close()
                return None
            buf = BytesIO()
            for chunk in r.iter_content(64 * 1024):
                if chunk:
                    buf.write(chunk)
                if buf.tell() > _MAX_BYTES:
                    r.close()
                    return None
            r.close()
            if not buf.tell():
                return None
            buf.seek(0)
            imagem = Image.open(buf)
            imagem.verify()
            buf.seek(0)
            return Image.open(buf).convert("RGB")
        return None
    except Exception as exc:  # rede, formato corrompido, etc. — colagem é best-effort
        logger.debug("Falha ao baixar imagem p/ colagem (%s): %s", url, exc)
        return None


def montar_colagem_b64(urls, max_itens=9):
    """Baixa as imagens e devolve (base64_jpeg, 'image/jpeg') com a colagem.

    ('', '') se nenhuma imagem baixar. A grade é a menor que cabe as fotos
    (2x2, 2x3, 3x3...). Cada foto entra reduzida, centralizada, sem distorcer.
    """
    imagens = []
    for url in urls:
        if len(imagens) >= max_itens:
            break
        img = _baixar_imagem(url)
        if img is not None:
            imagens.append(img)
    return _montar_imagens(imagens)


def montar_colagem_itens(itens, max_itens=9):
    """Colagem + somente os itens cujas fotos entraram nela."""
    imagens, validos = [], []
    for item in itens:
        if len(imagens) >= max_itens:
            break
        produto = item.get("produto")
        img = _baixar_imagem(getattr(produto, "imagem_url", ""))
        if img is not None:
            if str(getattr(produto, "marketplace", "")).casefold() == "amazon":
                img = _enquadrar_produto_amazon(img)
            imagens.append(img)
            validos.append(item)
    b64, mime = _montar_imagens(imagens)
    return b64, mime, validos


def _enquadrar_produto_amazon(img):
    """Amplia a miniatura e remove o excesso de branco sem cortar o produto.

    A Amazon pode devolver arquivos de apenas 226 px. ``thumbnail`` (usado na
    colagem) só reduz e, por isso, essas imagens ficavam no tamanho original dentro
    de células com mais de 500 px. O canvas quadrado em alta resolução permite a
    ampliação proporcional e mantém o fundo branco uniforme em qualquer grade.
    """
    from PIL import Image, ImageChops, ImageFilter

    rgb = img.convert("RGB")
    branco = Image.new("RGB", rgb.size, _FUNDO)
    diferenca = ImageChops.difference(rgb, branco).convert("L")
    mascara = diferenca.point(
        lambda pixel: 255 if pixel > _LIMIAR_FUNDO_BRANCO else 0,
    ).filter(ImageFilter.MedianFilter(size=5))
    bbox = mascara.getbbox()
    recorte = rgb
    if bbox:
        esquerda, topo, direita, base = bbox
        largura = direita - esquerda
        altura = base - topo
        respiro_x = max(4, round(largura * _RESPIRO_PRODUTO))
        respiro_y = max(4, round(altura * _RESPIRO_PRODUTO))
        caixa = (
            max(0, esquerda - respiro_x),
            max(0, topo - respiro_y),
            min(rgb.width, direita + respiro_x),
            min(rgb.height, base + respiro_y),
        )
        recorte = rgb.crop(caixa)

    alvo = _TELA - 2 * _MARGEM
    escala = min(alvo / recorte.width, alvo / recorte.height)
    tamanho = (
        max(1, round(recorte.width * escala)),
        max(1, round(recorte.height * escala)),
    )
    ampliado = recorte.resize(tamanho, Image.Resampling.LANCZOS)
    enquadrada = Image.new("RGB", (_TELA, _TELA), _FUNDO)
    enquadrada.paste(
        ampliado,
        ((_TELA - ampliado.width) // 2, (_TELA - ampliado.height) // 2),
    )
    return enquadrada


def _montar_imagens(imagens):
    from PIL import Image

    if not imagens:
        return "", ""

    n = len(imagens)
    colunas = math.ceil(math.sqrt(n))
    linhas = math.ceil(n / colunas)
    tela = Image.new("RGB", (_TELA, _TELA), _FUNDO)
    largura_cel = _TELA // colunas
    altura_cel = _TELA // linhas
    alvo_w = largura_cel - 2 * _MARGEM
    alvo_h = altura_cel - 2 * _MARGEM

    for i, img in enumerate(imagens):
        copia = img.copy()
        copia.thumbnail((alvo_w, alvo_h), Image.Resampling.LANCZOS)
        col = i % colunas
        lin = i // colunas
        ox = col * largura_cel + (largura_cel - copia.width) // 2
        oy = lin * altura_cel + (altura_cel - copia.height) // 2
        tela.paste(copia, (ox, oy))

    return preparar_jpeg_b64(tela)


def preparar_jpeg_b64(img):
    """PIL.Image -> (base64 JPEG, 'image/jpeg') dentro do teto de envio.

    Reduz o lado maior e, se ainda não couber, recomprime em qualidades
    decrescentes. A última qualidade é usada mesmo se não couber: mandar uma foto
    grande é melhor que mandar oferta sem foto, e o teto é uma margem de segurança
    do prazo de upload, não um limite rígido do WhatsApp.
    """
    from PIL import Image

    if max(img.size) > LADO_MAXIMO_ENVIO:
        img = img.copy()
        img.thumbnail((LADO_MAXIMO_ENVIO, LADO_MAXIMO_ENVIO), Image.Resampling.LANCZOS)

    dados = b""
    for qualidade in _QUALIDADES_JPEG:
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=qualidade, optimize=True)
        dados = buf.getvalue()
        if len(dados) <= MAX_BYTES_ENVIO:
            break
    else:
        logger.info("Foto ainda com %s bytes na menor qualidade; enviando assim.",
                    len(dados))
    return base64.b64encode(dados).decode("ascii"), "image/jpeg"
