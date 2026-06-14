"""
Amazon (amazon.com.br) — implementa o contrato Marketplace sobre a Creators API.

Link de afiliado é puro Python (?tag=), sem Playwright. Dados de oferta/preço vêm
da Creators API (sucessor da PA-API). Se a conta não tiver elegibilidade (10 vendas
qualificadas/30 dias), a API devolve 403; aqui isso é capturado em scrape_all para
NÃO derrubar o ML — só loga e pula o tick da Amazon.
"""
from apps.scrapers.marketplaces.base import Marketplace


class Amazon(Marketplace):
    slug = "amazon"

    def scrape_all(self, termos=None) -> None:
        """Amazon é POR USUÁRIO: cada um conecta a própria conta Creators e raspa as
        PRÓPRIAS ofertas (Produto.owner=user). Itera todos os usuários conectados.
        `termos` global é ignorado — usa os sub-nichos das configs de CADA usuário."""
        from apps.accounts.models import Perfil
        perfis = Perfil.objects.select_related("user").all()
        conectados = [p for p in perfis if p.amazon_conectado()]
        if not conectados:
            print("[amazon] Nenhum usuário com conta Amazon conectada — pulando.")
            return
        for perfil in conectados:
            self._scrape_usuario(perfil.user)

    def _scrape_usuario(self, usuario) -> None:
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
        except AmazonNotEligible as e:
            print(f"[amazon] user {usuario.id} não elegível (sem 10 vendas/30d?): {e} — pulando.")
        except AmazonConfigError as e:
            print(f"[amazon] user {usuario.id} config ausente: {e} — pulando.")

    def build_affiliate_link(self, produto, usuario=None):
        from apps.scrapers.scraper_amazon.link import gerar_link_afiliado_para_produto
        return gerar_link_afiliado_para_produto(produto, usuario=usuario)

    def verify_affiliate_tag(self, link, usuario=None):
        from apps.scrapers.scraper_amazon.link import link_tem_tag_afiliado
        return link_tem_tag_afiliado(link, usuario=usuario)

    def verify_link(self, link, nome_esperado=None, confiar_desconto=False):
        # Dados vêm da API oficial; confiamos. (ok=True como o default da base.)
        return {"ok": True}

    def is_alive(self, produto):
        """getItems(asin) com as creds do DONO do item: presente -> True; sumiu -> False."""
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
            print(f"[amazon] busca por termo pulada: {e}")
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
                print(f"[amazon-link] falha ASIN {getattr(p,'asin','?')}: {e}")
                falhas += 1
        return (gerados, falhas)
