"""
Mercado Livre — wrapper sobre o código existente (scrapers + link.py).
Sem mudança de comportamento: só expõe o que já existe pelo contrato Marketplace.
"""
import logging

from apps.scrapers.marketplaces.base import Marketplace

logger = logging.getLogger(__name__)


class MercadoLivre(Marketplace):
    slug = "mercadolivre"

    def scrape_all(self, termos=None) -> None:
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import (
            mapear_ofertas, buscar_por_termo,
        )
        from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import mapear_cupons_codigo

        mapear_ofertas(max_paginas=40)
        mapear_cupons_codigo()
        for t in (termos or []):
            try:
                buscar_por_termo(t)
            except Exception as e:
                logger.warning("Busca ML '%s' falhou: %s", t, e)

    def build_affiliate_link(self, produto, usuario=None):
        from apps.scrapers.scraper_mercadolivre.link import gerar_link_afiliado_para_produto
        return gerar_link_afiliado_para_produto(produto, usuario=usuario)

    def verify_affiliate_tag(self, link, usuario=None):
        from apps.scrapers.scraper_mercadolivre.link import link_tem_tag_afiliado
        return link_tem_tag_afiliado(link, usuario=usuario)

    def verify_link(self, link, nome_esperado=None, confiar_desconto=False, usuario=None):
        from apps.scrapers.scraper_mercadolivre.link import verificar_link_afiliado
        return verificar_link_afiliado(link, nome_esperado=nome_esperado,
                                       confiar_desconto=confiar_desconto,
                                       usuario=usuario)

    def is_alive(self, produto):
        from apps.scrapers.ofertas import esta_vivo
        return esta_vivo(produto)

    def buscar_por_termo(self, termo_busca, min_desconto=15, macro=None, usuario=None):
        # ML = pool COMPARTILHADO (owner=None p/ todos). Ignora usuario de propósito.
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import buscar_por_termo
        return buscar_por_termo(termo_busca, min_desconto=min_desconto, macro=macro)

    def prefetch_links(self, produtos):
        """Pré-gera links em lote (uma sessão Playwright). Retorna (gerados, falhas)."""
        from apps.scrapers.scraper_mercadolivre.link import gerar_links_em_lote
        return gerar_links_em_lote(produtos)
