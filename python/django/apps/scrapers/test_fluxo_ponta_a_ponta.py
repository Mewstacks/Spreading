"""Funis completos das duas lojas, do item coletado até a tela de envio.

Cada teste percorre o caminho inteiro com as funções REAIS do pipeline (coleta →
associação → preparo → link → verificação → projeção → tela), simulando apenas o
que sai da máquina: a rede das lojas e o Chromium. É o que separa "o código faz o
que o diff diz" de "o cupom chega a pronto".
"""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Perfil, ensure_personal_organization
from apps.scrapers.models import (
    CupomDisponibilidade, CupomNormalizado, CupomPreparacao, FonteIngestao,
    LinkAfiliadoUsuario, Produto, ProdutoCupom,
)
from apps.scrapers.sources.base import IngestedItem


COUPONS_URL = "https://www.amazon.com.br/deals?bubble-id=deals-collection-coupons"


class FluxoAmazonPontaAPontaTests(TestCase):
    """cupom oficial → produto → tag → link → pronto → tela."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("amazon-e2e")
        self.user.perfil.marcar_verificado()
        self.organization = ensure_personal_organization(self.user)
        Perfil.objects.update_or_create(
            user=self.user, defaults={"afiliado_tag_amazon": "loja-20"},
        )
        self.user.refresh_from_db()

    def _coletar_cupom_oficial(self):
        """Persiste exatamente o par (oferta, cupom) que AmazonCouponsSource emite."""
        from apps.scrapers.coupon_rules import normalizar_regras_cupom
        from apps.scrapers.sources.persistence import persist_items

        agora = timezone.now()
        oferta = IngestedItem(
            external_id="B0E2EAMZ01", marketplace="amazon",
            source="amazon-public-coupons", kind="offer",
            canonical_url="https://www.amazon.com.br/dp/B0E2EAMZ01",
            title="Fone com cupom", current_price=100.0, effective_price=80.0,
            reference_price=120.0, image_url="https://img/1.jpg",
            observed_at=agora,
            evidence={
                "transport": "amazon-official-deals",
                "association": "amazon-official-coupon-page",
                "coupon_final_price": 80.0,
                "promotion": {"present": True, "coupon_confirmed": True,
                              "id": "PROMO1", "label": "20% off"},
            },
        )
        cupom = IngestedItem(
            external_id="amazon-coupon:PROMO1", marketplace="amazon",
            source="amazon-public-coupons", kind="coupon",
            canonical_url=COUPONS_URL, title="Cupom Amazon — 20% OFF",
            coupon_rules=normalizar_regras_cupom(
                {"tipo_desconto": "porcentagem", "valor_desconto": 20,
                 "modo_resgate": "ativacao", "escopo": "produtos selecionados"},
                external_id="amazon-coupon:PROMO1"),
            content_type="promotion", observed_at=agora,
            evidence={
                "transport": "amazon-official-deals",
                "association": "amazon-official-coupon-page",
                "promotion_id": "PROMO1", "asins": ["B0E2EAMZ01"],
            },
        )
        return persist_items([oferta, cupom], owner=None)

    def test_funil_completo_ate_pronto_e_visivel(self):
        from apps.scrapers.coupon_pipeline import afiliar_cupons
        from apps.scrapers.coupon_products import preparar_lote
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        # 1) COLETA — a fonte oficial escreve oferta e cupom no catálogo público.
        contagens = self._coletar_cupom_oficial()
        self.assertEqual((contagens["offers"], contagens["coupons"]), (1, 1))
        produto = Produto.objects.get(marketplace="amazon", asin="B0E2EAMZ01")
        cupom = CupomNormalizado.objects.get(external_id="amazon-coupon:PROMO1")

        # 2) ASSOCIAÇÃO/PREPARO — os ASINs da promoção provam a relação.
        preparar_lote(limite=10, usuarios=[self.user], permitir_rede=False)
        relacao = ProdutoCupom.objects.get(cupom=cupom, produto=produto)
        self.assertEqual(relacao.status, "confirmado")
        self.assertEqual(float(relacao.preco_final), 80.0)
        self.assertEqual(
            CupomPreparacao.objects.get(cupom=cupom).status, "pronto")

        # 3) LINK + VERIFICAÇÃO — determinísticos, sem rede e sem Chromium.
        metricas = afiliar_cupons(self.user, limite=10)
        self.assertEqual(metricas["links_gerados"], 1)
        link = LinkAfiliadoUsuario.objects.get(usuario=self.user, produto=produto)
        self.assertIn("tag=loja-20", link.link_afiliado)
        self.assertIn("B0E2EAMZ01", link.link_afiliado)
        self.assertIs(link.verificado_ok, True)
        self.assertEqual(metricas["prontos"], 1)

        # 4) PROJEÇÃO — o funil publica "pronto" no mesmo ciclo.
        projecao = projetar_disponibilidade_cupons(self.user)
        self.assertEqual(projecao["stages"].get("ready"), 1)
        self.assertEqual(
            CupomDisponibilidade.objects.get(cupom=cupom, usuario=self.user).stage,
            "ready",
        )

        # 5) TELA — o cupom aparece na aba Cupons, e a oferta na aba de ofertas.
        self.client.force_login(self.user)
        cupons = self.client.get("/scrapers/top/?tipo=cupom")
        self.assertEqual(cupons.status_code, 200)
        self.assertIn(cupom.pk, [c.pk for c in cupons.context["cupons_catalogo"]])

        ofertas = self.client.get("/scrapers/top/?tipo=oferta")
        self.assertEqual(ofertas.status_code, 200)
        self.assertIn(produto.pk, [p.pk for p in ofertas.context["produtos"]])
        self.assertTrue(
            any(loja["slug"] == "amazon" and loja["prontos"] >= 1
                for loja in ofertas.context["prontos_por_loja"]),
            "a Amazon precisa aparecer no contador por loja",
        )

    def test_cupom_oficial_da_busca_tambem_chega_a_pronto(self):
        """A frase/preço final no card oficial é prova de ativação por ASIN."""
        from apps.scrapers.coupon_rules import normalizar_regras_cupom
        from apps.scrapers.coupon_products import preparar_lote
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons
        from apps.scrapers.sources.persistence import persist_items

        now = timezone.now()
        asin = "B0SEARCH01"
        promotion_id = f"search:{asin}"
        common_evidence = {
            "transport": "amazon-official-search",
            "association": "amazon-official-search-coupon",
            "coupon_final_price": 75.0,
        }
        offer = IngestedItem(
            external_id=asin, marketplace="amazon", source="amazon-public-web",
            kind="offer", canonical_url=f"https://www.amazon.com.br/dp/{asin}",
            title="Produto da busca com cupom", current_price=100.0,
            effective_price=75.0, reference_price=100.0,
            observed_at=now,
            evidence={
                **common_evidence,
                "promotion": {"present": True, "coupon_confirmed": True,
                              "id": promotion_id, "label": "25% off"},
            },
        )
        coupon = IngestedItem(
            external_id=f"amazon-search-coupon:{asin}", marketplace="amazon",
            source="amazon-public-web", kind="coupon",
            canonical_url=f"https://www.amazon.com.br/dp/{asin}",
            title="Cupom Amazon — 25% OFF", content_type="promotion",
            coupon_rules=normalizar_regras_cupom({
                "tipo_desconto": "porcentagem", "valor_desconto": 25,
                "modo_resgate": "ativacao", "escopo": "produto selecionado",
            }, external_id=f"amazon-search-coupon:{asin}"),
            observed_at=now,
            evidence={
                **common_evidence, "promotion_id": promotion_id, "asins": [asin],
            },
        )

        persisted = persist_items([offer, coupon])
        self.assertEqual(persisted["offers"], 1)
        self.assertEqual(persisted["coupons"], 1)
        preparar_lote(limite=10, usuarios=[self.user], permitir_rede=False)
        result = projetar_disponibilidade_cupons(self.user)

        normalized = CupomNormalizado.objects.get(
            fonte__slug="amazon-public-web", external_id=coupon.external_id,
        )
        self.assertEqual(result["stages"].get("ready"), 1)
        self.assertEqual(
            CupomDisponibilidade.objects.get(
                cupom=normalized, usuario=self.user,
            ).stage,
            "ready",
        )

    def test_sem_tag_a_tela_mostra_a_acao_em_vez_de_fila(self):
        """Configuração da conta não pode virar centenas de falhas de produto."""
        from apps.scrapers.coupon_pipeline import afiliar_cupons
        from apps.scrapers.coupon_products import preparar_lote

        Perfil.objects.filter(user=self.user).update(afiliado_tag_amazon="")
        self.user.refresh_from_db()
        self._coletar_cupom_oficial()
        preparar_lote(limite=10, usuarios=[self.user], permitir_rede=False)

        afiliar_cupons(self.user, limite=10)

        produto = Produto.objects.get(marketplace="amazon", asin="B0E2EAMZ01")
        self.assertFalse(
            LinkAfiliadoUsuario.objects.filter(
                usuario=self.user, produto=produto).exclude(tentativas=0).exists(),
            "nenhuma tentativa pode ser gasta por falta de tag",
        )
        self.client.force_login(self.user)
        resposta = self.client.get("/scrapers/top/?tipo=oferta&afiliado=todos")
        alvo = [p for p in resposta.context["produtos"] if p.pk == produto.pk]
        self.assertTrue(alvo)
        self.assertEqual(alvo[0].afiliado_estado, "sem_tag")
        self.assertIn("tag de afiliado da Amazon", alvo[0].afiliado_motivo)


class FluxoMercadoLivrePontaAPontaTests(TestCase):
    """campanha → evidência → produto → associação → link → verificação → pronto."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("ml-e2e")
        self.user.perfil.marcar_verificado()
        self.organization = ensure_personal_organization(self.user)
        self.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML"},
        )

    def _campanha_com_container(self):
        from apps.scrapers.coupon_products import atualizar_chave_cupom

        cupom = CupomNormalizado.objects.create(
            fonte=self.fonte, external_id="campanha:E2ECAMP",
            marketplace="mercadolivre", titulo="20% OFF em fones",
            link="https://lista.mercadolivre.com.br/_Container_fones",
            regras={
                "tipo_desconto": "porcentagem", "valor_desconto": 20,
                "modo_resgate": "ativacao",
                "container_url": "https://lista.mercadolivre.com.br/_Container_fones",
                "container_name": "fones",
            },
            evidencia={"association": "campaign",
                       "evidence_strength": "official_container"},
        )
        atualizar_chave_cupom(cupom)
        return cupom

    def _produto_do_container(self, cupom):
        produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Fone do container", origem="cupom",
            campanha_id="E2ECAMP", preco_sem_desconto=200.0, preco_com_cupom=150.0,
            link_produto="https://produto.mercadolivre.com.br/MLB-987654321",
            imagem_url="https://img/ml.jpg", estado="ativo",
        )
        ProdutoCupom.objects.create(
            produto=produto, cupom=cupom, status="confirmado",
            activation_key="E2ECAMP",
            verificado_em=timezone.now(),
            evidencia={"regra": "container", "item_id": "MLB987654321"},
        )
        return produto

    def test_funil_completo_ate_pronto(self):
        from apps.scrapers.coupon_pipeline import afiliar_cupons
        from apps.scrapers.coupon_products import preparar_lote
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons
        from apps.scrapers.coupon_rules import forca_evidencia

        cupom = self._campanha_com_container()
        produto = self._produto_do_container(cupom)

        # EVIDÊNCIA: container publicado pela fonte, não URL deduzida.
        self.assertEqual(forca_evidencia(cupom), "official_container")

        # PREPARO: preços do cupom são calculados sobre a associação comprovada.
        preparar_lote(limite=10, usuarios=[self.user], permitir_rede=False)
        relacao = ProdutoCupom.objects.get(cupom=cupom, produto=produto)
        self.assertEqual(relacao.status, "confirmado")
        self.assertEqual(float(relacao.preco_final), 120.0)

        # LINK + VERIFICAÇÃO: Link Builder e verificação de destino simulados.
        def _gerar(produtos, usuario=None, faixa=None, activation_keys=None):
            from apps.scrapers.afiliado import salvar_cache
            for item in produtos:
                activation = (activation_keys or {}).get(item.id, "")
                salvar_cache(usuario, item, "https://meli.la/e2e",
                             "https://produto.mercadolivre.com.br/MLB-987654321"
                             f"?coupon_campaign_id={activation}",
                             True)
            return (len(produtos), 0)

        @contextmanager
        def _browser_falso(*a, **kw):
            yield Mock(), Mock()

        @contextmanager
        def _recurso_livre(*a, **kw):
            yield True

        # Produto de cupom é julgado pela PDP DE ORIGEM (o short link do Programa
        # resolve para a vitrine /social/ do afiliado, nunca para o anúncio — ver
        # link_http.relatorio_de_link_com_cupom). Os dois transportes ficam
        # simulados para o teste não depender de qual fila o produto cai.
        with patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre."
                   "prefetch_links", side_effect=_gerar), \
                patch("apps.scrapers.scraper_mercadolivre.link.iniciar_browser",
                      _browser_falso), \
                patch("apps.scrapers.carga.browser_resource", _recurso_livre), \
                patch("apps.scrapers.scraper_mercadolivre.link_http."
                      "relatorio_de_link_com_cupom", return_value={"ok": True}), \
                patch("apps.scrapers.scraper_mercadolivre.link._relatorio_na_pagina",
                      return_value={"ok": True}):
            metricas = afiliar_cupons(self.user, limite=10)

        link = LinkAfiliadoUsuario.objects.get(usuario=self.user, produto=produto)
        self.assertIs(link.verificado_ok, True)
        self.assertEqual(metricas["links_verificados"], 1)
        self.assertEqual(metricas["prontos"], 1)

        # PROJEÇÃO + TELA.
        with patch("apps.scrapers.coupon_readiness.conexao_ml",
                   return_value={"ok": True, "reason": "", "detail": ""}):
            projecao = projetar_disponibilidade_cupons(self.user)
        self.assertEqual(projecao["stages"].get("ready"), 1)

        self.client.force_login(self.user)
        resposta = self.client.get("/scrapers/top/?tipo=cupom")
        self.assertIn(cupom.pk, [c.pk for c in resposta.context["cupons_catalogo"]])

    def test_sessao_invalida_nao_gasta_tentativa_dos_produtos(self):
        from apps.scrapers.coupon_pipeline import afiliar_cupons
        from apps.scrapers.coupon_products import preparar_lote
        from apps.scrapers.scraper_mercadolivre.link import LoginError

        cupom = self._campanha_com_container()
        produto = self._produto_do_container(cupom)
        preparar_lote(limite=10, usuarios=[self.user], permitir_rede=False)

        with patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre."
                   "prefetch_links", side_effect=LoginError("sessão caiu")):
            metricas = afiliar_cupons(self.user, limite=10)

        detalhe = metricas["por_marketplace"]["mercadolivre"]
        self.assertEqual(detalhe["reason_code"], "account_blocked:LoginError")
        self.assertFalse(
            LinkAfiliadoUsuario.objects.filter(
                usuario=self.user, produto=produto).exists(),
            "problema de sessão não pode criar falha por produto",
        )

    def test_reconectar_reabre_apenas_o_que_a_conta_travou(self):
        from apps.scrapers.afiliado import reabrir_bloqueios_de_conta

        cupom = self._campanha_com_container()
        produto = self._produto_do_container(cupom)
        outro = Produto.objects.create(
            marketplace="mercadolivre", nome="Catálogo /up/", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=80,
            link_produto="https://www.mercadolivre.com.br/up/MLBU123",
        )
        por_conta = LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto, estado="erro", tentativas=8,
            ultimo_erro="Falha operacional de afiliação (LoginError).",
            proxima_tentativa=timezone.now() + timezone.timedelta(hours=3),
        )
        do_produto = LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=outro, estado="nao_afiliavel", tentativas=1,
            ultimo_erro="O Programa de Afiliados não aceitou a URL deste produto.",
        )

        self.assertEqual(reabrir_bloqueios_de_conta(), 1)

        por_conta.refresh_from_db()
        do_produto.refresh_from_db()
        self.assertEqual(por_conta.estado, "pendente")
        self.assertEqual(por_conta.tentativas, 0)
        self.assertIsNone(por_conta.proxima_tentativa)
        self.assertEqual(do_produto.estado, "nao_afiliavel")
        self.assertEqual(do_produto.tentativas, 1)


class ContadoresDaTelaTests(TestCase):
    """O indicador tem de representar o mesmo universo que a tela lista."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("contadores")
        self.user.perfil.marcar_verificado()
        ensure_personal_organization(self.user)
        Perfil.objects.update_or_create(
            user=self.user, defaults={"afiliado_tag_amazon": "conta-20"},
        )
        self.user.refresh_from_db()
        for i in range(3):
            Produto.objects.create(
                marketplace="amazon", asin=f"B0CONTA{i:03d}", origem="oferta",
                nome=f"Amazon {i}", preco_sem_desconto=200, preco_com_cupom=150,
                link_produto=f"https://www.amazon.com.br/dp/B0CONTA{i:03d}",
                imagem_url="https://img/a.jpg", estado="ativo",
            )
        self.client.force_login(self.user)

    def test_modo_estrito_conta_prontos_e_reconcilia_com_a_lista(self):
        resposta = self.client.get("/scrapers/top/?tipo=oferta&afiliado=prontos")
        contexto = resposta.context
        self.assertTrue(contexto["contagem_estrita"])
        total = sum(l["prontos"] for l in contexto["prontos_por_loja"])
        self.assertEqual(total, contexto["page_obj"].paginator.count)

    def test_modo_diagnostico_nao_promete_prontidao_nao_apurada(self):
        """Com "mostrar pendentes" a prontidão não é resolvida no catálogo inteiro.

        Rotular aquela contagem como "pronto(s)" fazia a tela afirmar algo que não
        havia sido apurado — o indicador dizia 6 prontos numa conta sem tag Amazon,
        onde nenhum dos seis podia ser enviado.
        """
        resposta = self.client.get("/scrapers/top/?tipo=oferta&afiliado=todos")
        contexto = resposta.context
        self.assertFalse(contexto["contagem_estrita"])
        total = sum(l["prontos"] for l in contexto["prontos_por_loja"])
        self.assertEqual(total, contexto["page_obj"].paginator.count)

    def test_conta_sem_tag_oferece_a_acao_e_conta_como_fila(self):
        Perfil.objects.filter(user=self.user).update(afiliado_tag_amazon="")
        resposta = self.client.get("/scrapers/top/?tipo=oferta&afiliado=prontos")
        contexto = resposta.context
        self.assertTrue(contexto["acao_amazon_tag"])
        amazon = [l for l in contexto["prontos_por_loja"] if l["slug"] == "amazon"][0]
        self.assertEqual(amazon["prontos"], 0)
        self.assertEqual(amazon["pendentes"], 3)
        self.assertContains(resposta, "Cadastrar tag Amazon")


class SelecaoAutomaticaEquilibradaTests(TestCase):
    """A seleção automática não pode esconder uma loja inteira."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("selecao")
        ensure_personal_organization(self.user)

    def test_pool_de_cupons_e_formado_por_loja(self):
        from apps.scrapers.content_ranking import _coupon_candidates

        fonte_ml, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML", "status": "ok"},
        )
        fonte_az = FonteIngestao.objects.create(
            slug="amazon-public-coupons", marketplace="amazon",
            nome="Amazon", status="ok",
        )
        agora = timezone.now()
        # O ML é sempre o mais recente e em volume: numa amostragem global dos 80
        # mais recentes, a Amazon não entraria no pool.
        for i in range(90):
            CupomNormalizado.objects.create(
                fonte=fonte_ml, external_id=f"ml-{i}", marketplace="mercadolivre",
                titulo=f"Cupom ML {i}", codigo=f"MLCODE{i}",
                regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                        "valor_desconto": 20},
            )
        antigo = CupomNormalizado.objects.create(
            fonte=fonte_az, external_id="az-1", marketplace="amazon",
            titulo="Cupom Amazon", codigo="AZCODE1",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 30},
        )
        CupomNormalizado.objects.filter(pk=antigo.pk).update(
            ultima_observacao=agora - timezone.timedelta(hours=6))

        config = SimpleNamespace(
            owner=self.user, grupo_id="g@g.us", marketplace="", macro_categoria="",
            termo_busca="", horas_cooldown=24, min_desconto_percent=10,
            incluir_restritos=True, incluir_sem_desconto=True,
            programas=SimpleNamespace(values_list=lambda *a, **k: []),
        )
        with patch("apps.scrapers.coupon_products.ids_cupons_prontos") as prontos:
            # O que importa aqui é QUEM entra no pool; a prontidão é de outro teste.
            prontos.side_effect = lambda usuario, pool: {c.id for c in pool}
            candidatos = _coupon_candidates(config, limit=8)

        lojas = {c.obj.marketplace for c in candidatos}
        self.assertIn("amazon", lojas)
        self.assertIn("mercadolivre", lojas)

    def test_cupom_pronto_antigo_nao_e_expulso_por_backlog_pendente(self):
        """A fila nova sem validacao nao pode esconder o estoque ja pronto."""
        from apps.scrapers.content_ranking import _coupon_candidates

        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML", "status": "ok"},
        )
        agora = timezone.now()
        pronto = CupomNormalizado.objects.create(
            fonte=fonte, external_id="ml-pronto-antigo", marketplace="mercadolivre",
            titulo="Cupom validado que deve ser enviado", codigo="PRONTO25",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 25},
        )
        CupomNormalizado.objects.filter(pk=pronto.pk).update(
            ultima_observacao=agora - timezone.timedelta(hours=6),
        )
        for i in range(90):
            CupomNormalizado.objects.create(
                fonte=fonte, external_id=f"ml-pendente-{i}", marketplace="mercadolivre",
                titulo=f"Cupom ainda pendente {i}", codigo=f"PEND{i:03d}",
                regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                        "valor_desconto": 30},
            )
        CupomDisponibilidade.objects.create(
            organization=self.user.perfil.active_organization,
            usuario=self.user, cupom=pronto, channel="whatsapp",
            use_mode="code_notice", stage="ready",
        )
        config = SimpleNamespace(
            owner=self.user, grupo_id="g@g.us", marketplace="mercadolivre",
            macro_categoria="", termo_busca="", horas_cooldown=24,
            min_desconto_percent=10, incluir_restritos=True,
            incluir_sem_desconto=True,
            programas=SimpleNamespace(values_list=lambda *a, **k: []),
        )

        candidatos = _coupon_candidates(config, limit=8)

        self.assertEqual([c.obj.pk for c in candidatos], [pronto.pk])

    def test_comissao_shopee_nao_entra_no_ranking_como_desconto(self):
        from types import SimpleNamespace
        from apps.scrapers.content_ranking import _coupon_candidates

        fonte = FonteIngestao.objects.create(
            slug="shopee-campaigns", marketplace="shopee", nome="Shopee",
            status="ok",
        )
        CupomNormalizado.objects.create(
            fonte=fonte, owner=self.user,
            external_id="shopee:campanha:SHOP:x", marketplace="shopee",
            titulo="Campanha Shopee", codigo="",
            link="https://s.shopee.com.br/x",
            regras={"modo_resgate": "ativacao", "tipo_desconto": "porcentagem",
                    "valor_desconto": 12},
            evidencia={"transport": "shopee-affiliate-api"},
        )
        config = SimpleNamespace(
            owner=self.user, grupo_id="g@g.us", marketplace="", macro_categoria="",
            termo_busca="", horas_cooldown=24, min_desconto_percent=10,
            incluir_restritos=True, incluir_sem_desconto=True,
            programas=SimpleNamespace(values_list=lambda *a, **k: []),
        )
        with patch("apps.scrapers.coupon_products.ids_cupons_prontos",
                   return_value=set()):
            candidatos = _coupon_candidates(config, limit=8)
        self.assertNotIn("shopee", {c.obj.marketplace for c in candidatos})
