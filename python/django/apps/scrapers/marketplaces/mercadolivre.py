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

        from django.utils import timezone
        from apps.scrapers.models import FonteIngestao, ExecucaoIngestao
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web", defaults={
                "marketplace": "mercadolivre", "nome": "Mercado Livre — páginas públicas"})
        run = ExecucaoIngestao.objects.create(fonte=fonte)
        fonte.ultima_tentativa = timezone.now()
        fonte.save(update_fields=["ultima_tentativa"])
        try:
            ofertas = mapear_ofertas(max_paginas=40)
            cupons = mapear_cupons_codigo()
            for t in (termos or []):
                try:
                    buscar_por_termo(t)
                except Exception as e:
                    logger.warning("Busca ML '%s' falhou: %s", t, e)
        except Exception:
            now = timezone.now()
            run.status, run.finalizada_em = "error", now
            run.erro_publico = "Falha temporária na coleta; dados anteriores preservados."
            run.save(update_fields=["status", "finalizada_em", "erro_publico"])
            fonte.status, fonte.erro_publico = "degraded", run.erro_publico
            fonte.falhas_consecutivas += 1
            fonte.save(update_fields=["status", "erro_publico", "falhas_consecutivas"])
            raise
        total = ofertas + cupons
        now = timezone.now()
        run.status = "ok" if total else "empty"
        run.total_ofertas, run.total_cupons = ofertas, cupons
        run.finalizada_em = now
        run.save()
        fonte.ultimo_total = total
        fonte.status = "ok" if total else "degraded"
        fonte.erro_publico = "" if total else "Coleta vazia; catálogo anterior preservado."
        if total:
            fonte.ultimo_sucesso, fonte.falhas_consecutivas = now, 0
        fonte.save()

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
