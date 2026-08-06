"""
Amazon (amazon.com.br) — implementa o contrato Marketplace sobre a Creators API.

Link de afiliado é puro Python (?tag=), sem Playwright. Dados de oferta/preço vêm
da Creators API (sucessor da PA-API). Se a conta não tiver elegibilidade (10 vendas
qualificadas/30 dias), a API devolve 403; aqui isso é capturado em scrape_all para
NÃO derrubar o ML — só loga e pula o tick da Amazon.
"""
import logging

from apps.scrapers.marketplaces.base import Marketplace, MarketplaceIndisponivel

logger = logging.getLogger(__name__)


class Amazon(Marketplace):
    slug = "amazon"

    def scrape_all(self, termos=None) -> None:
        """Amazon é POR USUÁRIO: cada um conecta a própria conta Creators e raspa as
        PRÓPRIAS ofertas (Produto.owner=user). Itera todos os usuários conectados.
        `termos` global é ignorado — usa os sub-nichos das configs de CADA usuário."""
        from django.utils import timezone
        from apps.accounts.models import Perfil
        from apps.scrapers.afiliado import tag_amazon
        from apps.scrapers.scraper_amazon.creators_api import creds_de_usuario
        perfis = Perfil.objects.select_related("user").all()
        candidatos = [p for p in perfis if not p.bloqueado and tag_amazon(p.user)]
        conectados = [p for p in candidatos if creds_de_usuario(p.user).completo()]
        fallback = [p for p in candidatos if not creds_de_usuario(p.user).completo()]
        inicio = timezone.now()
        falhas = 0
        for perfil in conectados:
            if not self._scrape_usuario(perfil.user):
                fallback.append(perfil)
                falhas += 1
        if conectados:
            self._reportar_fonte(inicio, len(conectados), falhas)
        if fallback:
            self._scrape_publico([p.user for p in fallback], termos=termos)
        elif not conectados:
            logger.info("Nenhum usuario com tag Amazon; pulando")
        # Cupons públicos, preparo e links são mantidos pelo pipeline central de
        # cupons, independente do toggle desta raspagem geral.

    def scrape_para_usuario(self, usuario, termos=None) -> int:
        """Coleta a Amazon com a conta Creators de UM usuário.

        A Amazon é privada por usuário (Produto.owner = user), então este é o único
        caminho correto para uma raspagem pedida na tela: `scrape_all` percorreria
        as contas de todos os tenants.
        """
        from django.utils import timezone
        from apps.scrapers.afiliado import tag_amazon
        from apps.scrapers.models import Produto
        from apps.scrapers.scraper_amazon.creators_api import creds_de_usuario

        if not tag_amazon(usuario):
            raise MarketplaceIndisponivel(
                "sua tag de afiliado da Amazon não está cadastrada (tela Conta)")
        if not creds_de_usuario(usuario).completo():
            raise MarketplaceIndisponivel(
                "sua conta Amazon não está conectada (cadastre as credenciais na "
                "tela Conta)")

        inicio = timezone.now()
        if not self._scrape_usuario(usuario):
            from apps.accounts.models import Perfil
            perfil = Perfil.objects.filter(user=usuario).first()
            raise MarketplaceIndisponivel(
                (getattr(perfil, "amazon_ultimo_erro", "") or "").strip()
                or "a Amazon recusou a coleta agora")
        return Produto.objects.filter(
            marketplace="amazon", owner=usuario, ultima_observacao__gte=inicio,
        ).count()

    def _scrape_usuario(self, usuario) -> bool:
        from apps.scrapers.models import ConfiguracaoEnvio
        from apps.scrapers.scraper_amazon import ofertas_scraper as az
        from apps.scrapers.scraper_amazon.creators_api import (
            AmazonAPIError, AmazonConfigError, AmazonNotEligible,
        )
        termos = list(
            ConfiguracaoEnvio.objects.filter(owner=usuario, ativo=True)
            .exclude(termo_busca="").values_list("termo_busca", flat=True)
        )
        try:
            # Uma coleta, dois destinos: ofertas e promoções liam as MESMAS páginas
            # da API separadamente e dobravam o consumo da cota do usuário.
            itens = az.coletar_feed(usuario)
            az.mapear_ofertas(usuario=usuario, itens=itens)
            az.mapear_cupons_codigo(usuario=usuario, itens=itens)
            for t in termos:
                az.buscar_por_termo(t, usuario=usuario)
        except AmazonNotEligible as e:
            logger.info("Usuario %s nao elegivel para Amazon Creators API: %s", usuario.id, e)
            self._marcar_elegibilidade(usuario, False,
                                       "Conta sem elegibilidade na Creators API (10 vendas/30 dias).")
            return False
        except AmazonConfigError as e:
            logger.info("Configuracao Amazon ausente para usuario %s: %s", usuario.id, e)
            self._marcar_elegibilidade(usuario, None, f"Configuração Amazon incompleta: {e}")
            return False
        except AmazonAPIError as e:
            # A API recusou TODAS as buscas (cota estourada, host errado, fora do ar).
            # Isto era engolido keyword a keyword: o ciclo terminava "com sucesso" e
            # zero itens, a conta era marcada elegível, o fallback público nunca
            # rodava e o painel dizia apenas "nenhuma oferta confirmada".
            logger.warning("Coleta Amazon do usuario %s falhou por completo: %s",
                           usuario.id, e)
            self._marcar_elegibilidade(usuario, None, f"Amazon indisponível: {e}")
            return False
        self._marcar_elegibilidade(usuario, True, "")
        return True

    @staticmethod
    def _reportar_fonte(inicio, contas, falhas):
        """Publica o resultado do ciclo na tela de Fontes.

        Nada escrevia nesta linha: a Creators API aparecia como `degraded`, com
        `ultimo_total=0` e `ultimo_sucesso` vazio, para sempre — mesmo tendo
        coletado centenas de produtos. O painel dizia que a fonte estava quebrada
        justamente enquanto ela funcionava, e uma quebra real ficava
        indistinguível do estado normal.
        """
        from django.utils import timezone
        from apps.scrapers.models import FonteIngestao, Produto

        agora = timezone.now()
        total = Produto.objects.filter(
            marketplace="amazon", fonte="amazon-creators-api",
            ultima_observacao__gte=inicio,
        ).count()
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="amazon-creators-api",
            defaults={"marketplace": "amazon", "nome": "Amazon — Creators API"},
        )
        fonte.ultima_tentativa = agora
        fonte.ultimo_total = total
        if falhas >= contas:
            fonte.status = "degraded"
            fonte.falhas_consecutivas += 1
            fonte.erro_publico = (
                "Nenhuma conta Amazon concluiu a coleta; catálogo anterior preservado."
            )
        elif falhas:
            fonte.status = "degraded"
            fonte.ultimo_sucesso = agora
            fonte.falhas_consecutivas = 0
            fonte.erro_publico = (
                f"{falhas} de {contas} conta(s) Amazon não concluíram a coleta."
            )
        else:
            fonte.status = "ok" if total else "degraded"
            fonte.ultimo_sucesso = agora if total else fonte.ultimo_sucesso
            fonte.falhas_consecutivas = 0
            fonte.erro_publico = "" if total else (
                "Coleta vazia; catálogo anterior preservado.")
        fonte.save(update_fields=[
            "ultima_tentativa", "ultimo_total", "status", "ultimo_sucesso",
            "falhas_consecutivas", "erro_publico",
        ])

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
    def _scrape_cupons_publicos(usuarios):
        from apps.scrapers.sources import run_source
        from apps.scrapers.sources.persistence import persist_items

        resultado = run_source("amazon-public-coupons")
        itens = resultado.get("offers", []) + resultado.get("coupons", [])
        if not itens:
            return
        for usuario in usuarios:
            persist_items(itens, owner=usuario)

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
        # Dados de oferta/preço vêm da Creators API (fonte oficial). Itens de origem
        # confiável ('oferta'/'busca', confiar_desconto=True) NÃO precisam de raspagem:
        # a checagem pública headless levava CAPTCHA/timeout ou seletor mudado a reprovar
        # links perfeitamente válidos ("link reprovado na verificação"). Confiamos direto.
        if confiar_desconto:
            return {"ok": True, "motivo": "confiado (Creators API)"}
        # Origem não confiável (ex.: cupom_codigo): tenta confirmar na PDP, mas trata
        # CAPTCHA/timeout/DOM ausente como INCONCLUSIVO -> aprova (não bloqueia envio).
        # Só reprova em indisponibilidade explícita do produto.
        from apps.scrapers.sources.amazon_public import verify_product_url
        try:
            resultado = verify_product_url(link, nome_esperado=nome_esperado)
        except Exception as e:
            logger.warning("Verificação pública Amazon falhou (inconclusivo, aprovando): %s", e)
            return {"ok": True, "motivo": "verificação inconclusiva"}
        if not resultado.get("ok"):
            motivo = (resultado.get("motivo") or "").lower()
            if "indisponível" in motivo or "indisponivel" in motivo:
                return resultado  # produto realmente indisponível -> reprova
            logger.info("Verificação Amazon inconclusiva (%s); aprovando", resultado.get("motivo"))
            return {"ok": True, "motivo": f"inconclusivo: {resultado.get('motivo')}"}
        return resultado

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
        if not listing:
            return False
        # A mesma resposta já traz preço fresco; descartá-lo era o que deixava a
        # mensagem publicar valor de até 48h atrás (a idade que expire_stale tolera).
        from apps.scrapers.scraper_amazon.ofertas_scraper import _mapear_item
        try:
            mapeado = _mapear_item(itens[0])
        except Exception:
            mapeado = None
        if mapeado and mapeado.get("preco_com_cupom", 0) > 0:
            from django.utils import timezone
            from apps.scrapers import precos
            produto.preco_com_cupom = mapeado["preco_com_cupom"]
            produto.preco_sem_desconto = mapeado["preco_sem_desconto"]
            produto.preco_efetivo = mapeado["preco_com_cupom"]
            produto.ultima_verificacao = timezone.now()
            produto.save(update_fields=[
                "preco_com_cupom", "preco_sem_desconto", "preco_efetivo",
                "ultima_verificacao",
            ])
            precos.registrar("amazon", asin, produto.link_produto,
                             mapeado["preco_com_cupom"])
        return True

    def buscar_por_termo(self, termo_busca, min_desconto=15, macro=None, usuario=None):
        """Busca da tela de Promoções. Falha explica-se; nunca vira um zero mudo.

        O `except` daqui era código morto: `ofertas_scraper.buscar_por_termo` engolia
        toda exceção termo a termo e devolvia 0. Quem clicava "Buscar nas lojas" lia
        "amazon: 0 item(ns)" tanto para "não há oferta" quanto para "sua conta Amazon
        não está conectada" — e não havia como distinguir os dois.
        """
        from apps.scrapers.scraper_amazon.ofertas_scraper import buscar_por_termo
        from apps.scrapers.scraper_amazon.creators_api import (
            AmazonConfigError, AmazonNotEligible,
        )
        try:
            return buscar_por_termo(termo_busca, min_desconto=min_desconto,
                                    macro=macro, usuario=usuario)
        except AmazonConfigError as e:
            logger.info("Busca por termo Amazon pulada: %s", e)
            raise MarketplaceIndisponivel(
                "conta Amazon não conectada (cadastre credenciais e tag na tela Conta)"
            ) from e
        except AmazonNotEligible as e:
            logger.info("Busca por termo Amazon pulada: %s", e)
            raise MarketplaceIndisponivel(
                "conta Amazon sem elegibilidade na Creators API (10 vendas em 30 dias)"
            ) from e

    def can_affiliate(self, produto, usuario=None) -> bool:
        from apps.scrapers.scraper_amazon.link import pode_gerar_link
        return pode_gerar_link(produto, usuario=usuario)

    def preparar_exibicao(self, produtos, usuario=None) -> None:
        """Link em cache vale tanto quanto tag+ASIN — e resolve a página em 1 query.

        O default da base pergunta item a item por `can_affiliate`, que na Amazon só
        olha a tag do Perfil: um item JÁ afiliado (link gravado em LinkAfiliadoUsuario)
        sumia da listagem se a tag fosse removida depois, mesmo com o link funcionando.
        """
        from apps.scrapers.afiliado import situacao_dos_links

        situacao = situacao_dos_links(usuario, produtos)
        for p in produtos:
            info = situacao.get(p.id) or {}
            cacheado = bool(info.get("link_afiliado") or getattr(p, "link_afiliado", ""))
            p.afiliado_pronto = cacheado or self.can_affiliate(p, usuario)
            if p.afiliado_pronto:
                p.afiliado_estado, p.afiliado_motivo = "pronto", ""
            elif info:
                p.afiliado_estado = info["estado"]
                p.afiliado_motivo = info["ultimo_erro"]
            else:
                p.afiliado_estado, p.afiliado_motivo = "pendente", ""

    def prefetch_links(self, produtos, usuario=None, faixa=None):
        # `faixa` não se aplica: o link Amazon é montado em memória (tag + ASIN), a
        # etapa é instantânea e não tem o que reportar numa barra.
        from apps.scrapers.scraper_amazon.link import gerar_link_afiliado_para_produto
        from apps.scrapers.afiliado import registrar_falha
        gerados = falhas = 0
        for p in produtos:
            try:
                if gerar_link_afiliado_para_produto(p, usuario=usuario):
                    gerados += 1
                else:
                    registrar_falha(
                        usuario, p,
                        "Tag Amazon ou URL canônica não configurada.",
                    )
                    falhas += 1
            except Exception as e:
                logger.warning("Falha ao gerar link Amazon para ASIN %s: %s", getattr(p, "asin", "?"), e)
                registrar_falha(usuario, p, str(e))
                falhas += 1
        return (gerados, falhas)
