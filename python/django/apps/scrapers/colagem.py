"""Colagem de fotos de produtos numa imagem única, p/ a mensagem de cupom.

A mensagem de cupom (ver `ofertas.montar_mensagem_cupom_produtos`) mostra vários
produtos de uma vez; o WhatsApp só manda UMA imagem por mensagem, então as fotos
dos produtos são reduzidas e coladas numa grade sobre fundo branco — a cliente vê
tudo de relance, no formato pedido.

Depende só de Pillow (já usado em `ofertas._baixar_imagem_b64`).
"""
import base64
import logging
import math
from io import BytesIO

import requests

logger = logging.getLogger(__name__)

_TELA = 1080          # lado da imagem final (quadrada, padrão de card)
_MARGEM = 12          # respiro branco entre as células
_FUNDO = (255, 255, 255)


def _baixar_imagem(url):
    """URL -> PIL.Image RGB, ou None. Mesma conversão de _baixar_imagem_b64 (webp falha)."""
    if not url or not str(url).startswith("http"):
        return None
    try:
        from PIL import Image
        r = requests.get(url, timeout=8)
        if r.status_code != 200 or not r.content:
            return None
        return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception as exc:  # rede, formato corrompido, etc. — colagem é best-effort
        logger.debug("Falha ao baixar imagem p/ colagem (%s): %s", url, exc)
        return None


def montar_colagem_b64(urls, max_itens=9):
    """Baixa as imagens e devolve (base64_jpeg, 'image/jpeg') com a colagem.

    ('', '') se nenhuma imagem baixar. A grade é a menor que cabe as fotos
    (2x2, 2x3, 3x3...). Cada foto entra reduzida, centralizada, sem distorcer.
    """
    from PIL import Image

    imagens = []
    for url in urls:
        if len(imagens) >= max_itens:
            break
        img = _baixar_imagem(url)
        if img is not None:
            imagens.append(img)
    if not imagens:
        return "", ""

    n = len(imagens)
    if n == 1:
        # Uma foto só: não vira grade, entrega a própria imagem em JPEG.
        return _para_b64(imagens[0])

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

    return _para_b64(tela)


def _para_b64(img):
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
