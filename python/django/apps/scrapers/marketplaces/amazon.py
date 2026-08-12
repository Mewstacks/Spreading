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
        from apps.scrapers.scraper_amazon.ofertas_scraper import metricas_vazias

        inicio = timezone.now()
        falhas = 0
        metricas = metricas_vazias()
        for perfil in conectados:
            if not self._scrape_usuario(perfil.user, metricas=metricas):
                fallback.append(perfil)
                falhas += 1
        if conectados:
            metricas["contas"] = len(conectados)
            metricas["contas_com_falha"] = falhas
            self._reportar_fonte(inicio, len(conectados), falhas, metricas=metricas)
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

        from apps.scrapers.scraper_amazon.ofertas_scraper import metricas_vazias

        inicio = timezone.now()
        metricas = metricas_vazias()
        ok = self._scrape_usuario(usuario, metricas=metricas)
        # A tela de Fontes também precisa saber desta coleta. Sem isto o badge só
        # se movia no ciclo do worker: raspar pela tela funcionava e o painel
        # continuava dizendo "Atenção".
        metricas["contas"] = 1
        metricas["contas_com_falha"] = 0 if ok else 1
        self._reportar_fonte(inicio, 1, 0 if ok else 1, metricas=metricas)
        if not ok:
            from apps.accounts.models import Perfil
            perfil = Perfil.objects.filter(user=usuario).first()
            raise MarketplaceIndisponivel(
                (getattr(perfil, "amazon_ultimo_erro", "") or "").strip()
                or "a Amazon recusou a coleta agora")
        return Produto.objects.filter(
            marketplace="amazon", owner=usuario, ultima_observacao__gte=inicio,
        ).count()

    def _scrape_usuario(self, usuario, metricas=None) -> bool:
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
            itens = az.coletar_feed(usuario, metricas=metricas)
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
    def _reportar_fonte(inicio, contas, falhas, metricas=None):
        """Publica o resultado do ciclo na tela de Fontes.

        Nada escrevia nesta linha: a Creators API aparecia como `degraded`, com
        `ultimo_total=0` e `ultimo_sucesso` vazio, para sempre — mesmo tendo
        coletado centenas de produtos. O painel dizia que a fonte estava quebrada
        justamente enquanto ela funcionava, e uma quebra real ficava
        indistinguível do estado normal.

        Chamada tanto pelo worker (`scrape_all`) quanto pela raspagem pedida na tela
        (`scrape_para_usuario`). Só o worker reportava, então o usuário podia raspar
        com sucesso o dia inteiro sem o badge sair de "Atenção".
        """
        from django.utils import timezone
        from apps.scrapers.models import ExecucaoIngestao, FonteIngestao, Produto

        agora = timezone.now()
        total = Produto.objects.filter(
            marketplace="amazon", fonte="amazon-creators-api",
            ultima_observacao__gte=inicio,
        ).count()
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="amazon-creators-api",
            # Mesmo rótulo semeado pela migration 0031: a busca é por slug, mas um
            # banco novo que rodasse o scraper antes dela mostrava outro nome na tela.
            defaults={"marketplace": "amazon", "nome": "Amazon Creators API"},
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
            # Toda conta respondeu: a fonte está de pé. Ciclo sem item NOVO é
            # rotina (o feed da Creators API repete itens já observados e a janela
            # começa em `inicio`), não avaria — marcar `degraded` aqui deixava o
            # badge âmbar em operação perfeitamente normal.
            fonte.status = "ok"
            fonte.ultimo_sucesso = agora
            fonte.falhas_consecutivas = 0
            fonte.erro_publico = "" if total else (
                "Coleta concluída sem itens novos; catálogo anterior preservado.")
        fonte.save(update_fields=[
            "ultima_tentativa", "ultimo_total", "status", "ultimo_sucesso",
            "falhas_consecutivas", "erro_publico",
        ])
        if metricas is None:
            return
        # COBERTURA: quantas palavras-chave, páginas e chamadas sustentaram este
        # número. "6 ofertas" pode ser o catálogo inteiro ou o teto da varredura —
        # sem isto não há como saber qual, nem decidir se vale ampliar a taxonomia.
        ExecucaoIngestao.objects.create(
            fonte=fonte, status="ok" if total else "empty",
            finalizada_em=agora, total_ofertas=total,
            paginas_processadas=int(metricas.get("paginas") or 0),
            metricas=metricas,
            rejeicoes=dict(metricas.get("erros_por_tipo") or {}),
            health_status=(
                "degraded" if falhas else
                "partial" if metricas.get("paginas_no_teto") else "ok"
            ),
        )

    @staticmethod
    def _scrape_publico(usuarios, termos=None):
        """Catálogo PÚBLICO: coletado uma vez e compartilhado (`owner=None`).

        Antes o mesmo resultado público era persistido uma vez POR USUÁRIO elegível:
        N cópias idênticas da mesma oferta, N vezes o custo de escrita e um catálogo
        que crescia com o número de contas em vez de com o número de ofertas. O que
        de fato é privado por usuário é o LINK (tag de afiliado), e ele continua em
        `LinkAfiliadoUsuario` — a coleta não precisa ser duplicada para isso.

        `usuarios` permanece na assinatura porque é ele que diz se ALGUÉM está
        elegível a esta coleta; nenhuma linha é escrita no escopo deles.
        """
        from django.conf import settings
        if not getattr(settings, "AMAZON_PUBLIC_FALLBACK", True):
            return
        if not usuarios:
            return
        from apps.scrapers.sources import run_source
        from apps.scrapers.sources.persistence import persist_items
        resultado = run_source("amazon-public-web", terms=termos)
        persist_items(resultado.get("offers", []), owner=None)

    @staticmethod
    def _scrape_cupons_publicos(usuarios):
        from apps.scrapers.sources import run_source
        from apps.scrapers.sources.persistence import persist_items

        resultado = run_source("amazon-public-coupons")
        itens = resultado.get("offers", []) + resultado.get("coupons", [])
        if not itens:
            return
        persist_items(itens, owner=None)

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
        # Origem não confiável precisa de comprovação positiva. CAPTCHA/timeout/DOM
        # ausente ficam em espera; aprová-los transformava falha operacional em
        # evidência de elegibilidade.
        from apps.scrapers.sources.amazon_public import verify_product_url
        try:
            resultado = verify_product_url(link, nome_esperado=nome_esperado)
        except Exception as e:
            logger.warning("Verificação pública Amazon falhou (inconclusivo): %s", e)
            return {"ok": False, "motivo": "verificação inconclusiva", "transitorio": True}
        if not resultado.get("ok"):
            motivo = (resultado.get("motivo") or "").lower()
            if "indisponível" in motivo or "indisponivel" in motivo:
                return resultado  # produto realmente indisponível -> reprova
            logger.info("Verificação Amazon inconclusiva (%s)", resultado.get("motivo"))
            return {"ok": False,
                    "motivo": f"inconclusivo: {resultado.get('motivo')}",
                    "transitorio": True}
        return resultado

    @staticmethod
    def _avisar_tag_ausente(usuario, quantos):
        """UM evento por conta e por janela, com a ação que destrava a fila.

        Sem o cooldown seriam dezenas de eventos por dia por conta desconfigurada, e
        a tela de Saúde afogaria justamente no aviso que precisa ser lido — mesmo
        desenho de `_avisar_sem_sessao_ml` na lane de links do ML.
        """
        from django.core.cache import cache
        from apps.scrapers.eventos import log_event

        if usuario is None:
            return
        chave = f"amazon_tag_missing:{getattr(usuario, 'pk', '')}"
        if cache.get(chave):
            return
        cache.set(chave, True, timeout=6 * 3600)
        log_event(
            "scraper", "amazon_tag_missing",
            f"{quantos} oferta(s) da Amazon estão prontas, mas sem a tag de "
            "afiliado nenhum link comissiona. Cadastre a tag na tela Conta.",
            level="warning", usuario=usuario,
            contexto={"servico": "Amazon", "produtos": quantos,
                      "acao": "Cadastrar tag Amazon"},
        )

    def verificar_links_pendentes(self, usuario, limite=40, produto_ids=None):
        from apps.scrapers.scraper_amazon.link import verificar_links_pendentes
        return verificar_links_pendentes(
            usuario, limite=limite, produto_ids=produto_ids,
        )

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
        """Prontidão da Amazon em 1 query, com o MESMO respeito ao veredito do ML.

        Duas regras convivem aqui, e a diferença entre elas não é inconsistência:

        - Um veredito REPROVADO (`verificado_ok=False`) manda, sempre. Antes bastava
          existir link em cache para o item aparecer enviável; um link reprovado
          (produto fora do ar) continuava sendo oferecido e só falhava no clique.
        - Na AUSÊNCIA de veredito, tag + ASIN bastam. O link da Amazon é
          determinístico — `https://.../dp/{ASIN}?tag={tag}` é montado em memória no
          próprio envio —, então a prova de atribuição existe antes de qualquer
          linha no banco. É o contrário do Mercado Livre, onde a URL vem do Link
          Builder e só a verificação de destino diz se ela abre o anúncio certo.
        """
        from apps.scrapers.afiliado import situacao_dos_links, tag_amazon

        situacao = situacao_dos_links(usuario, produtos)
        for p in produtos:
            info = situacao.get(p.id) or {}
            verificado = info.get("verificado_ok") if info else None
            cacheado = bool(info.get("link_afiliado") or getattr(p, "link_afiliado", ""))
            if verificado is False:
                p.afiliado_pronto = False
                p.afiliado_estado = "link_invalido"
                p.afiliado_motivo = (info.get("verificacao_motivo")
                                     or info.get("ultimo_erro") or "")
                continue
            p.afiliado_pronto = (
                verificado is True or cacheado or self.can_affiliate(p, usuario)
            )
            if p.afiliado_pronto:
                p.afiliado_estado, p.afiliado_motivo = "pronto", ""
            elif info:
                p.afiliado_estado = info["estado"]
                p.afiliado_motivo = info["ultimo_erro"]
            else:
                # Sem tag cadastrada não há link possível — e isso é configuração da
                # conta, não fila. A tela precisa oferecer a ação, não uma espera.
                p.afiliado_estado, p.afiliado_motivo = (
                    ("sem_tag", "Cadastre a tag de afiliado da Amazon na tela Conta.")
                    if not tag_amazon(usuario) else ("pendente", "")
                )

    def prefetch_links(self, produtos, usuario=None, faixa=None):
        # `faixa` não se aplica: o link Amazon é montado em memória (tag + ASIN), a
        # etapa é instantânea e não tem o que reportar numa barra.
        from apps.scrapers.scraper_amazon.link import gerar_link_afiliado_para_produto
        from apps.scrapers.afiliado import registrar_falha, tag_amazon
        gerados = falhas = 0
        if produtos and not tag_amazon(usuario):
            # GATE DE CONTA: a tag é uma só e vale para o catálogo inteiro. Sem ela,
            # cada produto ganhava uma "falha" própria — centenas de linhas em
            # backoff por um campo que ninguém preencheu na tela Conta. Um aviso, e
            # nenhuma tentativa gasta: quando a tag existir, a fila anda sozinha.
            logger.info(
                "Links Amazon pulados para %s: tag de afiliado não cadastrada "
                "(%s produto(s) intactos).", getattr(usuario, "pk", None),
                len(produtos),
            )
            self._avisar_tag_ausente(usuario, len(produtos))
            return (0, 0)
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
