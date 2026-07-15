"""
Amazon (amazon.com.br) — implementa o contrato Marketplace sobre a Creators API.

Link de afiliado é puro Python (?tag=), sem Playwright. Dados de oferta/preço vêm
da Creators API (sucessor da PA-API). Se a conta não tiver elegibilidade (10 vendas
qualificadas/30 dias), a API devolve 403; aqui isso é capturado em scrape_all para
NÃO derrubar o ML — só loga e pula o tick da Amazon.
"""
import logging

from apps.scrapers.marketplaces.base import Marketplace

logger = logging.getLogger(__name__)


class Amazon(Marketplace):
    slug = "amazon"

    def scrape_all(self, termos=None) -> None:
        """Amazon é POR USUÁRIO: cada um conecta a própria conta Creators e raspa as
        PRÓPRIAS ofertas (Produto.owner=user). Itera todos os usuários conectados.
        `termos` global é ignorado — usa os sub-nichos das configs de CADA usuário."""
        from apps.accounts.models import Perfil
        from apps.scrapers.afiliado import tag_amazon
        from apps.scrapers.scraper_amazon.creators_api import creds_de_usuario
        perfis = Perfil.objects.select_related("user").all()
        candidatos = [p for p in perfis if not p.bloqueado and tag_amazon(p.user)]
        conectados = [p for p in candidatos if creds_de_usuario(p.user).completo()]
        fallback = [p for p in candidatos if not creds_de_usuario(p.user).completo()]
        for perfil in conectados:
            if not self._scrape_usuario(perfil.user):
                fallback.append(perfil)
        if fallback:
            self._scrape_publico([p.user for p in fallback], termos=termos)
        elif not conectados:
            logger.info("Nenhum usuario com tag Amazon; pulando")

    def _scrape_usuario(self, usuario) -> bool:
        from apps.scrapers.models import ConfiguracaoEnvio
        from apps.scrapers.scraper_amazon import ofertas_scraper as az
        from apps.scrapers.scraper_amazon.creators_api import (
            AmazonNotEligible, AmazonConfigError,
        )
        termos = list(
            ConfiguracaoEnvio.objects.filter(owner=usuario, ativo=True)
            .exclude(termo_busca="").values_list("termo_busca", flat=True)
        )
        try:
            az.mapear_ofertas(usuario=usuario)
            az.mapear_cupons_codigo(usuario=usuario)
            for t in termos:
                az.buscar_por_termo(t, usuario=usuario)
            self._marcar_elegibilidade(usuario, True, "")
            return True
        except AmazonNotEligible as e:
            logger.info("Usuario %s nao elegivel para Amazon Creators API: %s", usuario.id, e)
            self._marcar_elegibilidade(usuario, False,
                                       "Conta sem elegibilidade na Creators API (10 vendas/30 dias).")
            return False
        except AmazonConfigError as e:
            logger.info("Configuracao Amazon ausente para usuario %s: %s", usuario.id, e)
            self._marcar_elegibilidade(usuario, None, f"Configuração Amazon incompleta: {e}")

            return False

    @staticmethod
    def _scrape_publico(usuarios, termos=None):
        from django.conf import settings
        if not getattr(settings, "AMAZON_PUBLIC_FALLBACK", True):
            return
        from apps.scrapers.sources import run_source
        from apps.scrapers.sources.persistence import persist_items
        resultado = run_source("amazon-public-web", terms=termos)
        for usuario in usuarios:
            persist_items(resultado.get("offers", []), owner=usuario)

    @staticmethod
    def _marcar_elegibilidade(usuario, elegivel, msg):
        """Persiste o resultado da raspagem Amazon no Perfil (exibido no painel)."""
        from apps.accounts.models import Perfil
        Perfil.objects.filter(user=usuario).update(
            amazon_elegivel=elegivel, amazon_ultimo_erro=msg[:255]
        )

    def build_affiliate_link(self, produto, usuario=None):
        from apps.scrapers.scraper_amazon.link import gerar_link_afiliado_para_produto
        return gerar_link_afiliado_para_produto(produto, usuario=usuario)

    def verify_affiliate_tag(self, link, usuario=None):
        from apps.scrapers.scraper_amazon.link import link_tem_tag_afiliado
        return link_tem_tag_afiliado(link, usuario=usuario)

    def verify_link(self, link, nome_esperado=None, confiar_desconto=False, usuario=None):
        # Dados vêm da API oficial; confiamos. (ok=True como o default da base.)
        from apps.scrapers.sources.amazon_public import verify_product_url
        return verify_product_url(link, nome_esperado=nome_esperado)

    def is_alive(self, produto):
        """getItems(asin) com as creds do DONO do item: presente -> True; sumiu -> False."""
        if getattr(produto, "fonte", "") == "amazon-public-web":
            from apps.scrapers.sources.amazon_public import AmazonPublicSource
            from apps.scrapers.sources.base import IngestedItem
            item = IngestedItem(
                external_id=produto.asin, marketplace="amazon", source="amazon-public-web",
                kind="offer", canonical_url=produto.link_produto, title=produto.nome,
                current_price=produto.preco_com_cupom,
                reference_price=produto.preco_sem_desconto,
            )
            try:
                refreshed = AmazonPublicSource().refresh_offer(item)
            except Exception:
                return None
            if refreshed is None:
                return False
            produto.preco_com_cupom = refreshed.current_price
            from django.utils import timezone
            produto.ultima_verificacao = timezone.now()
            produto.save(update_fields=["preco_com_cupom", "ultima_verificacao"])
            return True
        from apps.scrapers.scraper_amazon import creators_api
        asin = getattr(produto, "asin", "")
        if not asin:
            return None
        creds = creators_api.creds_de_usuario(getattr(produto, "owner", None))
        try:
            itens = creators_api.get_items([asin], creds=creds)
        except creators_api.AmazonNotEligible:
            return None
        except Exception:
            return None
        if not itens:
            return False
        listing = (itens[0].get("offersV2", {}) or {}).get("listings") or []
        return True if listing else False

    def buscar_por_termo(self, termo_busca, min_desconto=15, macro=None, usuario=None):
        from apps.scrapers.scraper_amazon.ofertas_scraper import buscar_por_termo
        from apps.scrapers.scraper_amazon.creators_api import (
            AmazonNotEligible, AmazonConfigError,
        )
        try:
            return buscar_por_termo(termo_busca, min_desconto=min_desconto,
                                    macro=macro, usuario=usuario)
        except (AmazonNotEligible, AmazonConfigError) as e:
            logger.info("Busca por termo Amazon pulada: %s", e)
            return 0

    def prefetch_links(self, produtos, usuario=None):
        from apps.scrapers.scraper_amazon.link import gerar_link_afiliado_para_produto
        gerados = falhas = 0
        for p in produtos:
            try:
                if gerar_link_afiliado_para_produto(p, usuario=usuario):
                    gerados += 1
                else:
                    falhas += 1
            except Exception as e:
                logger.warning("Falha ao gerar link Amazon para ASIN %s: %s", getattr(p, "asin", "?"), e)
                falhas += 1
        return (gerados, falhas)
