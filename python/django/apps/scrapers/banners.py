"""Banners fixos do broadcast de cupons, sorteados a cada mensagem.

O aviso de cupons novos (`ofertas.enviar_aviso_cupons`) não tem foto de produto —
ele não anuncia produto nenhum. A imagem é um banner da loja, e são vários por loja
de propósito: a mesma arte repetida em toda mensagem faz o grupo parar de olhar.

Os arquivos são versionados no repositório e valem para todos os tenants (decisão
do produto). Diretório vazio não é erro: a mensagem simplesmente sai em texto, e o
envio continua. Ver o README de cada pasta para o formato esperado.
"""
import logging
import random
from pathlib import Path

logger = logging.getLogger(__name__)

RAIZ = Path(__file__).resolve().parent / "assets" / "banners"

# Só formatos que o Pillow abre e o transporte aceita depois de recomprimir.
EXTENSOES = (".jpg", ".jpeg", ".png", ".webp")

# Pasta por marketplace. O slug do `CupomNormalizado.marketplace` já é este texto.
_PASTAS = {
    "mercadolivre": "mercadolivre",
    "amazon": "amazon",
}


def caminhos_disponiveis(marketplace: str) -> list:
    """Banners existentes para a loja, em ordem estável. [] quando não há nenhum."""
    pasta = _PASTAS.get(str(marketplace or "").strip().lower())
    if not pasta:
        return []
    diretorio = RAIZ / pasta
    if not diretorio.is_dir():
        return []
    return sorted(
        caminho for caminho in diretorio.iterdir()
        if caminho.is_file() and caminho.suffix.lower() in EXTENSOES
    )


def sortear_banner_b64(marketplace: str):
    """(base64 JPEG, mimetype) de um banner sorteado, ou (None, None).

    Passa pelo mesmo `preparar_jpeg_b64` da colagem: é ele que garante o teto de
    bytes que o worker do WhatsApp consegue subir dentro do orçamento de envio —
    uma imagem grande demais não vira erro, vira entrega "incerta".
    """
    caminhos = caminhos_disponiveis(marketplace)
    if not caminhos:
        return None, None
    escolhido = random.choice(caminhos)
    try:
        from PIL import Image
        from apps.scrapers.colagem import preparar_jpeg_b64

        with Image.open(escolhido) as img:
            # JPEG não tem canal alfa: um PNG/WebP transparente vira erro no save
            # se não for convertido antes.
            return preparar_jpeg_b64(img.convert("RGB"))
    except Exception:
        logger.warning("Banner %s não pôde ser preparado; a mensagem sai sem imagem.",
                       escolhido.name, exc_info=True)
        return None, None
