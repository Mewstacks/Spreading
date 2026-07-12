"""Re-linkagem de mensagens de canais curados (B4).

Extrai URLs de produto (ML/Amazon) do texto de uma mensagem e troca cada uma pela
versão com a tag de afiliado do DONO — reaproveitando os geradores de link já
existentes (ML via Link Builder/Playwright; Amazon via string /dp/{ASIN}?tag=).
"""
import hashlib
import logging
import re

logger = logging.getLogger(__name__)

_ML_URL = re.compile(
    r"https?://[^\s]*(?:mercadolivre\.com|mercadolibre\.com|meli\.la)[^\s]*", re.I
)
_AMZ_URL = re.compile(
    r"https?://(?:www\.)?(?:amazon\.com\.br|amazon\.com|amzn\.to|amzn\.eu)[^\s]*", re.I
)
_ASIN = re.compile(r"/(?:dp|gp/product|d)/([A-Z0-9]{10})", re.I)


def extrair_urls(texto):
    """Lista de (url, marketplace) encontradas no texto. ML e Amazon."""
    t = texto or ""
    achados = [(u, "mercadolivre") for u in _ML_URL.findall(t)]
    achados += [(u, "amazon") for u in _AMZ_URL.findall(t)]
    return achados


def _asin_de_url(url):
    m = _ASIN.search(url or "")
    return m.group(1).upper() if m else ""


def hash_url(url):
    return hashlib.sha256((url or "").strip().encode()).hexdigest()


def gerar_link_afiliado(url, marketplace, usuario):
    """URL-fonte -> link afiliado do usuário. None se não der (sem tag/asin/sessão)."""
    if marketplace == "amazon":
        asin = _asin_de_url(url)
        if not asin:  # shortlink (amzn.to) sem asin visível — não resolve aqui
            return None
        from django.conf import settings
        from apps.scrapers.afiliado import tag_amazon
        tag = tag_amazon(usuario)
        if not tag:
            return None
        host = getattr(settings, "AMAZON_MARKETPLACE", "www.amazon.com.br")
        return f"https://{host}/dp/{asin}?tag={tag}"

    # Mercado Livre — Link Builder com a sessão do usuário, verificando a tag (A3).
    from apps.scrapers.scraper_mercadolivre.link import (
        afiliate_link_builder, _auth_path, link_tem_tag_afiliado,
    )
    link = afiliate_link_builder(url, auth_path=_auth_path(usuario))
    if link and link_tem_tag_afiliado(link, usuario=usuario):
        return link
    return None


def reescrever_mensagem(texto, usuario):
    """Troca cada URL de produto pela afiliada do usuário.
    Retorna (novo_texto, chaves_enviadas) — chaves = hashes das URLs re-linkadas."""
    novo = texto or ""
    chaves = []
    for url, mkt in extrair_urls(texto):
        try:
            af = gerar_link_afiliado(url, mkt, usuario)
        except Exception as e:
            logger.warning("Falha ao gerar link afiliado para URL de canal: %s", e)
            af = None
        if af:
            novo = novo.replace(url, af)
            chaves.append(hash_url(url))
    return novo, chaves
