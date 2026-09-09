from datetime import timedelta
from importlib import import_module
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import OperationalError, connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import Organization
from apps.scrapers.models import (
    ConfiguracaoEnvio, Cupom, CupomDisponibilidade, CupomFonteObservacao,
    CupomNormalizado, CupomPreparacao, FonteIngestao, LinkAfiliadoUsuario,
    LinkAfiliadoProdutoCupomUsuario, Produto, ProdutoCupom,
)


class CouponSemanticParserTests(SimpleTestCase):
    def test_codigo_alfabetico_exige_evidencia_semantica(self):
        from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import (
            _extrair_codigos, _extrair_codigos_semanticos,
        )

        page = Mock()
        page.locator.return_value.all_inner_texts.return_value = [
            "Código: PROMOMELI — copiar",
            "Eletrônicos Samsung OFERTA",
        ]

        self.assertNotIn("PROMOMELI", _extrair_codigos("PROMOMELI Samsung"))
        self.assertEqual(_extrair_codigos_semanticos(page), ["PROMOMELI"])


class CouponMessageReadinessTests(SimpleTestCase):
    @patch("apps.scrapers.coupon_readiness.coupon_mode_enabled", return_value=True)
    def test_codigo_solto_nunca_fica_pronto_para_mensagem(self, _enabled):
        from apps.scrapers.coupon_readiness import _codigo

        cupom = SimpleNamespace(
            pk=17, codigo="PAREADO20", programa=None, integracao=None,
            regras={"modo_resgate": "codigo"},
        )

        resultado = _codigo(cupom, usuario=None, conexao={"ok": True}, prontas={})

        self.assertEqual(
            (resultado["stage"], resultado["reason_code"]),
            ("eligible", "product_match_pending"),
        )


class CouponWorkerOrderingTests(SimpleTestCase):
    def test_worker_projects_checkout_results_only_after_pipeline(self):
        """A coleta/preparo/link precisa terminar antes da validação opcional."""
        from apps.scrapers.management.commands.automacao import _rodar_cupons

        pipeline = {
            "encontrados": 0, "persistidos": 0, "preparados": 0,
            "links_verificados": 0, "prontos": 0,
            "falhos": 0, "links_falhos": 0,
        }
        with patch(
            "apps.scrapers.coupon_pipeline.executar_pipeline_cupons",
            return_value=pipeline,
        ) as executar, patch(
            "apps.scrapers.categorizar_por_nome.popular_macro_por_nome",
            return_value=7,
        ), patch(
            "apps.scrapers.coupon_validation_runner.defer_missing_checkout_sessions",
            return_value=3,
        ), patch(
            "apps.scrapers.coupon_validation_runner.run_validation_batch",
            return_value={"confirmados": 1},
        ):
            resultado = _rodar_cupons(lote=20)

        executar.assert_called_once_with(coletar=True, limite_preparo=20, limite_links=20)
        self.assertEqual(resultado["validacoes_sem_sessao"], 3)
        self.assertEqual(resultado["validacoes_checkout"], {"confirmados": 1})


class CouponPersistenceRetryTests(TestCase):
    def test_retry_persiste_sem_refazer_a_varredura(self):
        from apps.scrapers.scraper_mercadolivre.scraper import (
            _persistir_campanhas_cupons,
        )

        rows = [{
            "campaignId": "retry-1", "title": "Cupom retry",
            "desconto": {"tipo": "porcentagem", "valor": 10},
            "valor_minimo": 0, "link_produtos": "https://lista.mercadolivre.com.br/",
            "codigo": "",
        }]
        original = Cupom.objects.bulk_create
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OperationalError("the connection is closed")
            return original(*args, **kwargs)

        with patch.object(Cupom.objects, "bulk_create", side_effect=flaky), \
                patch(
                    "apps.scrapers.scraper_mercadolivre.scraper.renovar_conexoes"
                ) as close, \
                patch("apps.scrapers.scraper_mercadolivre.scraper.time.sleep"):
            total = _persistir_campanhas_cupons(
                rows, varredura_completa=False, pagina_final=1,
            )

        self.assertEqual(total, 1)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(close.call_count, 2)
        self.assertTrue(Cupom.objects.filter(campanha_id="retry-1").exists())

    def test_reconciliacao_completa_nao_regrava_expirados_inalterados(self):
        from apps.scrapers.scraper_mercadolivre.scraper import (
            _persistir_campanhas_cupons,
        )

        antigo = timezone.now() - timedelta(days=3)
        motivo = "Cupom não observado na última sincronização"
        cupom_morto = Cupom.objects.create(
            campanha_id="dead-coupon", titulo="Já expirado",
            valor_desconto=10, estado="expirado", ultima_verificacao=antigo,
        )
        produto_morto = Produto.objects.create(
            marketplace="mercadolivre", origem="cupom",
            campanha_id="dead-product", nome="Produto já expirado",
            preco_sem_desconto=100, preco_com_cupom=90,
            link_produto="https://produto.mercadolivre.com.br/MLB-1",
            estado="expirado", falha_verificacao=motivo,
            ultima_verificacao=antigo,
        )
        rows = [{
            "campaignId": "live-coupon", "title": "Cupom vigente",
            "desconto": {"tipo": "porcentagem", "valor": 10},
            "valor_minimo": 0, "link_produtos": "https://lista.mercadolivre.com.br/",
            "codigo": "", "estado": "ativo",
        }]

        _persistir_campanhas_cupons(rows, varredura_completa=True)

        cupom_morto.refresh_from_db()
        produto_morto.refresh_from_db()
        self.assertEqual(cupom_morto.ultima_verificacao, antigo)
        self.assertEqual(produto_morto.ultima_verificacao, antigo)


class CouponContractsMigrationTests(TestCase):
    def test_backfill_preserva_observacao_de_fonte_independente(self):
        organization = Organization.objects.create(
            name="ML system", slug="ml-system-migration",
        )
        legacy_source = FonteIngestao.objects.get(slug="mercadolivre-web")
        authenticated_source = FonteIngestao.objects.get(
            slug="mercadolivre-campanhas"
        )
        corroborating_source = FonteIngestao.objects.create(
            slug="ml-corroborating-test", marketplace="mercadolivre",
            nome="ML corroboracao",
        )
        coupon = CupomNormalizado.objects.create(
            fonte=legacy_source, external_id="campanha:migration-1",
            marketplace="mercadolivre", titulo="Campanha autenticada",
            evidencia={"association": "campaign"},
        )
        legacy_observation = CupomFonteObservacao.objects.create(
            fonte=legacy_source, cupom=coupon, canonical_key="coupon:migration-1",
            source_external_id="migration-1",
        )
        corroborating_observation = CupomFonteObservacao.objects.create(
            fonte=corroborating_source, cupom=coupon,
            canonical_key="coupon:migration-1", source_external_id="migration-1",
        )

        migration = import_module(
            "apps.scrapers.migrations.0061_coupon_pipeline_contracts"
        )
        with override_settings(ML_SYSTEM_ORGANIZATION_ID=str(organization.pk)):
            migration.backfill_coupon_contracts(
                import_module("django.apps").apps,
                SimpleNamespace(connection=connection),
            )

        coupon.refresh_from_db()
        legacy_observation.refresh_from_db()
        corroborating_observation.refresh_from_db()
        self.assertEqual(coupon.fonte, authenticated_source)
        self.assertEqual(coupon.organization, organization)
        self.assertEqual(legacy_observation.fonte, authenticated_source)
        self.assertEqual(legacy_observation.organization, organization)
        self.assertEqual(corroborating_observation.fonte, corroborating_source)
        self.assertIsNone(corroborating_observation.organization)
        self.assertEqual(CupomFonteObservacao.objects.filter(cupom=coupon).count(), 2)


class CouponPipelineTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("pipeline-user")
        self.source = FonteIngestao.objects.create(
            slug="pipeline-ml", marketplace="mercadolivre", nome="Pipeline ML",
        )
        self.coupon = CupomNormalizado.objects.create(
            fonte=self.source, external_id="pipeline-coupon",
            marketplace="mercadolivre", titulo="Cupom 20%",
            codigo="PIPE20", estado="ativo",
            regras={
                "modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                "valor_desconto": 20, "site_wide": True,
            },
        )

    def _prepared_products(self):
        from apps.scrapers.coupon_products import atualizar_chave_cupom

        products = []
        for index, origem in enumerate(("oferta", "busca", "cupom", "cupom_codigo")):
            product = Produto.objects.create(
                marketplace="mercadolivre", nome=f"Produto {origem}",
                origem=origem, estado="ativo",
                preco_sem_desconto=100, preco_com_cupom=80,
                link_produto=f"https://produto.mercadolivre.com.br/MLB-{index}",
                imagem_url=f"https://img.example/{index}.jpg",
            )
            ProdutoCupom.objects.create(
                produto=product, cupom=self.coupon, status="confirmado",
                verificado_em=timezone.now(), preco_original=100,
                preco_atual=80, preco_final=64,
            )
            products.append(product)
        CupomPreparacao.objects.create(
            cupom=self.coupon, usuario=None, status="pronto",
            produtos_chave=atualizar_chave_cupom(self.coupon),
            verificado_em=timezone.now(),
        )
        return products

    def test_afilia_produtos_relacionados_de_todas_as_origens_e_verifica_no_ciclo(self):
        from apps.scrapers.coupon_pipeline import afiliar_cupons

        products = self._prepared_products()

        def generate(items, usuario=None, faixa=None):
            for product in items:
                LinkAfiliadoUsuario.objects.create(
                    usuario=usuario, produto=product, estado="pronto",
                    afiliado_ok=True, link_afiliado=f"https://meli.la/{product.id}",
                    verificado_ok=None,
                )
            return len(items), 0

        def verify(usuario, limite=20, produto_ids=None):
            LinkAfiliadoUsuario.objects.filter(
                usuario=usuario, produto_id__in=produto_ids,
            ).update(verificado_ok=True, verificado_em=timezone.now())
            return {"aprovados": len(produto_ids), "reprovados": 0, "transitorios": 0}

        # A verificação é pedida ao ADAPTADOR da loja, nunca ao verificador de um
        # marketplace específico: é esse roteamento que impede link Amazon de ser
        # julgado pela regra do Mercado Livre.
        verifier = Mock(side_effect=verify)
        marketplace = SimpleNamespace(
            prefetch_links=generate, verificar_links_pendentes=verifier,
        )

        with patch(
            "apps.scrapers.marketplaces.registry.get_marketplace",
            return_value=marketplace,
        ):
            result = afiliar_cupons(self.user, limite=10)

        self.assertEqual(result["vinculados"], 4)
        self.assertEqual(result["links_gerados"], 4)
        self.assertEqual(result["links_verificados"], 4)
        self.assertEqual(result["prontos"], 1)
        self.assertEqual(
            set(verifier.call_args.kwargs["produto_ids"]),
            {product.id for product in products},
        )

    def test_link_gerado_sem_veredito_nao_libera_cupom(self):
        from apps.scrapers.coupon_products import ids_cupons_prontos

        product = self._prepared_products()[0]
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=product, estado="pronto",
            afiliado_ok=True, link_afiliado="https://meli.la/pendente",
            verificado_ok=None,
        )

        self.assertEqual(ids_cupons_prontos(self.user, [self.coupon]), set())

    def test_pares_confirmados_geram_link_antes_da_coleta(self):
        from apps.scrapers.coupon_pipeline import executar_pipeline_cupons, _metricas_vazias

        ordem = []
        afiliacao = {
            "vinculados": 1, "links_gerados": 1, "links_verificados": 1,
            "links_reprovados": 0, "links_transitorios": 0, "links_falhos": 0,
            "prontos": 1,
        }
        with patch(
            "apps.scrapers.coupon_pipeline._usuarios_ativos", return_value=[self.user],
        ), patch(
            "apps.scrapers.coupon_pipeline.afiliar_cupons",
            side_effect=lambda *args, **kwargs: ordem.append("links") or afiliacao,
        ), patch(
            "apps.scrapers.coupon_pipeline.coletar_cupons",
            side_effect=lambda **kwargs: ordem.append("coleta") or _metricas_vazias(),
        ), patch(
            "apps.scrapers.coupon_products.preparar_lote",
            return_value={"processados": 0, "prontos": 0, "por_fonte": {},
                          "adiados_sem_browser": 0},
        ), patch(
            "apps.scrapers.coupon_readiness.projetar_disponibilidade_cupons",
            return_value={},
        ), patch(
            "apps.scrapers.coupon_validation.agendar_lote_validacao", return_value={},
        ), patch(
            "apps.scrapers.coupon_links.rastreio_afiliado_ml", return_value=True,
        ):
            resultado = executar_pipeline_cupons(usuarios=[self.user])

        self.assertEqual(ordem[:2], ["links", "coleta"])
        self.assertEqual(resultado["links_verificados"], 1)

    def test_conta_com_destino_tem_prioridade_sobre_conta_de_teste(self):
        from apps.scrapers.coupon_pipeline import _priorizar_usuarios_com_destino

        teste = get_user_model().objects.create_user("pipeline-teste")
        ConfiguracaoEnvio.objects.create(
            owner=self.user, organization=self.user.perfil.organization,
            grupo_id="teste@g.us", grupo_nome="Teste ofertas", ativo=False,
        )

        ordenados = _priorizar_usuarios_com_destino([teste, self.user])

        self.assertEqual(ordenados, [self.user, teste])

    def test_conta_do_piloto_tem_prioridade_mesmo_com_destino_de_teste(self):
        """O único Chromium serve a operação piloto antes de uma conta auxiliar."""
        from apps.scrapers.coupon_pipeline import _priorizar_usuarios_com_destino

        auxiliar = get_user_model().objects.create_user("pipeline-auxiliar")
        ConfiguracaoEnvio.objects.create(
            owner=auxiliar, organization=auxiliar.perfil.organization,
            grupo_id="auxiliar@g.us", grupo_nome="Teste auxiliar", ativo=False,
        )

        with override_settings(
            PILOT_ORGANIZATION_IDS={str(self.user.perfil.organization_id)},
        ):
            ordenados = _priorizar_usuarios_com_destino([auxiliar, self.user])

        self.assertEqual(ordenados, [self.user, auxiliar])

    def test_macro_do_destino_tem_prioridade_na_fila_de_links(self):
        """O primeiro lote deve servir um grupo configurado antes do catálogo solto."""
        from apps.scrapers.coupon_pipeline import afiliar_cupons

        products = self._prepared_products()
        products[0].macro_categoria = "Casa, Móveis e Decoração"
        products[0].save(update_fields=["macro_categoria"])
        products[1].macro_categoria = "Celulares, Telefonia e Wearables"
        products[1].save(update_fields=["macro_categoria"])
        ConfiguracaoEnvio.objects.create(
            owner=self.user, organization=self.user.perfil.organization,
            grupo_id="teste@g.us", grupo_nome="Teste ofertas", ativo=False,
            macro_categoria="Celulares, Telefonia e Wearables",
        )
        marketplace = Mock()
        marketplace.prefetch_links.return_value = (0, 0)
        marketplace.verificar_links_pendentes.return_value = {
            "reprovados": 0, "transitorios": 0,
        }

        with patch(
            "apps.scrapers.marketplaces.registry.get_marketplace",
            return_value=marketplace,
        ):
            afiliar_cupons(self.user, limite=1)

        self.assertEqual(
            marketplace.prefetch_links.call_args.args[0][0].pk,
            products[1].pk,
        )

    def test_codigo_com_maior_desconto_tem_prioridade_de_link(self):
        from apps.scrapers.coupon_pipeline import _peso_do_cupom

        menor = CupomNormalizado.objects.create(
            fonte=self.source, external_id="pipeline-low-discount",
            marketplace="mercadolivre", titulo="Cupom 5%", codigo="PIPE5",
            estado="ativo", regras={
                "modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                "valor_desconto": 5,
            },
        )

        self.assertLess(_peso_do_cupom(self.coupon), _peso_do_cupom(menor))

    def test_pipeline_nao_gasta_browser_com_codigo_sem_produto(self):
        from apps.scrapers.coupon_pipeline import afiliar_cupons

        with patch(
            "apps.scrapers.coupon_pipeline.afiliar_cupons_de_codigo",
            side_effect=AssertionError("código solto não pode gerar link"),
        ):
            resultado = afiliar_cupons(self.user, limite=1)

        self.assertEqual(resultado["links_gerados"], 0)
        self.assertEqual(resultado["cupons_codigo_pendentes"], 0)

    def test_afiliacao_nao_faz_queries_por_cupom(self):
        from apps.scrapers.coupon_pipeline import afiliar_cupons
        from apps.scrapers.coupon_products import chave_produtos_cupom

        products = self._prepared_products()
        for product in products:
            LinkAfiliadoUsuario.objects.create(
                usuario=self.user, produto=product, estado="pronto",
                afiliado_ok=True, link_afiliado=f"https://meli.la/{product.id}",
                verificado_ok=True,
            )
        codigo_vazio = {"gerados": 0, "falhas": 0, "pendentes": 0}
        with patch(
            "apps.scrapers.coupon_pipeline.afiliar_cupons_de_codigo",
            return_value=codigo_vazio,
        ):
            with CaptureQueriesContext(connection) as poucas:
                afiliar_cupons(self.user, limite=10)

            for index in range(20):
                coupon = CupomNormalizado.objects.create(
                    fonte=self.source, external_id=f"pipeline-extra-{index}",
                    marketplace="mercadolivre", titulo=f"Cupom {index}",
                    codigo=f"EXTRA{index}", estado="ativo",
                    regras={"modo_resgate": "codigo", "tipo_desconto": "fixo",
                            "valor_desconto": 5, "site_wide": True},
                )
                ProdutoCupom.objects.create(
                    produto=products[0], cupom=coupon, status="confirmado",
                    verificado_em=timezone.now(), preco_original=100,
                    preco_atual=80, preco_final=75,
                )
                CupomPreparacao.objects.create(
                    cupom=coupon, usuario=None, status="pronto",
                    produtos_chave=chave_produtos_cupom(coupon),
                    verificado_em=timezone.now(),
                )

            with CaptureQueriesContext(connection) as muitas:
                afiliar_cupons(self.user, limite=10)

        self.assertLessEqual(len(muitas), len(poucas) + 1)

    def test_preparo_faz_rodizio_entre_fontes(self):
        from apps.scrapers.coupon_products import preparar_lote

        other_source = FonteIngestao.objects.create(
            slug="pipeline-small", marketplace="mercadolivre", nome="Fonte pequena",
        )
        for index in range(5):
            CupomNormalizado.objects.create(
                fonte=self.source, external_id=f"large-{index}",
                marketplace="mercadolivre", titulo=f"Grande {index}",
                codigo=f"LARGE{index}", estado="ativo",
                regras={"modo_resgate": "codigo"},
            )
        small = CupomNormalizado.objects.create(
            fonte=other_source, external_id="small",
            marketplace="mercadolivre", titulo="Pequeno", codigo="SMALL1",
            estado="ativo", regras={"modo_resgate": "codigo"},
        )

        with patch(
            "apps.scrapers.coupon_products.preparar_cupom", return_value=[object()],
        ) as prepare:
            result = preparar_lote(limite=2, permitir_rede=False)

        self.assertEqual(result["processados"], 2)
        self.assertIn(small.id, {call.args[0].id for call in prepare.call_args_list})

    def test_amazon_e_awin_persistem_veredito_uniforme(self):
        amazon = Produto.objects.create(
            owner=self.user, marketplace="amazon", asin="B012345678",
            nome="Amazon", origem="oferta", preco_sem_desconto=100,
            preco_com_cupom=80, link_produto="https://www.amazon.com.br/dp/B012345678",
        )
        self.user.perfil.afiliado_tag_amazon = "minhatag-20"
        self.user.perfil.save(update_fields=["afiliado_tag_amazon"])

        from apps.scrapers.marketplaces.amazon import Amazon
        from apps.scrapers.marketplaces.awin import Awin

        self.assertEqual(Amazon().prefetch_links([amazon], usuario=self.user), (1, 0))
        self.assertIs(
            LinkAfiliadoUsuario.objects.get(
                usuario=self.user, produto=amazon,
            ).verificado_ok,
            True,
        )

        awin = Produto.objects.create(
            owner=self.user, marketplace="awin", asin="AW123",
            nome="Awin", origem="oferta", preco_sem_desconto=100,
            preco_com_cupom=80,
            link_produto="https://www.awin1.com/cread.php?awinmid=1",
        )
        self.assertEqual(Awin().prefetch_links([awin], usuario=self.user), (1, 0))
        self.assertIs(
            LinkAfiliadoUsuario.objects.get(
                usuario=self.user, produto=awin,
            ).verificado_ok,
            True,
        )

    @override_settings(AFFILIATE_FEED_URL="", AWIN_INTEGRATION_ENABLED=False)
    @patch("apps.scrapers.sources.run_source")
    def test_falha_de_uma_fonte_nao_interrompe_o_ciclo(self, run_source):
        from apps.scrapers.coupon_pipeline import coletar_cupons

        run_source.return_value = {
            "status": "error", "offers": [], "coupons": [],
            "error": "payload alterado",
        }
        result = coletar_cupons(usuarios=[self.user])

        # Contador derivado, não número mágico: com um literal, ADICIONAR uma fonte
        # quebrava este teste sem que nada tivesse regredido. O que ele mede é que o
        # ciclo termina apesar das falhas, não quantas fontes existem hoje.
        com_erro = [
            slug for slug, dados in result["fontes"].items()
            if dados["status"] == "error"
        ]
        self.assertEqual(result["falhos"], len(com_erro))
        self.assertGreaterEqual(len(com_erro), 2)
        self.assertEqual(
            result["fontes"]["ml-cupons-afiliados"]["status"], "error",
        )
        self.assertEqual(
            result["fontes"]["amazon-public-coupons"]["status"], "error",
        )
        # E o ciclo chegou ao fim: fontes que não passam por `run_source` continuam
        # sendo reportadas depois das que falharam.
        self.assertIn("manual-private", result["fontes"])


class CouponNicheRankingTests(TestCase):
    def test_macro_da_regra_aceita_produto_confirmado_do_cupom(self):
        """A campanha não precisa repetir o nicho no próprio título.

        A prova relevante é a relação confirmada com o produto, que é também o
        único caminho que o transporte aceita publicar.
        """
        from apps.scrapers.content_ranking import _coupon_candidates

        user = get_user_model().objects.create_user("ranking-nicho", password="x")
        fonte = FonteIngestao.objects.create(
            slug="ranking-nicho-fonte", marketplace="mercadolivre", nome="Cupons")
        config = ConfiguracaoEnvio.objects.create(
            owner=user, grupo_id="teste@g.us", grupo_nome="Teste ofertas",
            canal="whatsapp", macro_categoria="Eletrodomésticos",
            min_desconto_percent=15,
        )
        cupom = CupomNormalizado.objects.create(
            fonte=fonte, external_id="ranking-nicho-20", marketplace="mercadolivre",
            categoria="Campanhas gerais", titulo="Oferta da semana", codigo="CASA20",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 20}, estado="ativo",
            ultima_observacao=timezone.now(),
        )
        produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Air fryer comprovada", origem="cupom",
            macro_categoria="Eletrodomésticos", preco_sem_desconto=300,
            preco_com_cupom=240, link_produto="https://produto.example/airfryer",
            imagem_url="https://img.example/airfryer.jpg",
        )
        ProdutoCupom.objects.create(
            produto=produto, cupom=cupom, status="confirmado",
            preco_original=300, preco_atual=300, preco_final=240,
            verificado_em=timezone.now(),
        )
        CupomDisponibilidade.objects.create(
            organization=user.perfil.organization, usuario=user, cupom=cupom,
            channel="whatsapp", stage="ready", use_mode="code_notice",
        )

        candidatos = _coupon_candidates(config, limit=5)

        self.assertEqual([item.obj.pk for item in candidatos], [cupom.pk])

    def test_termo_da_regra_aceita_nome_do_produto_confirmado(self):
        """Um cupom de titulo generico deve aparecer pelo produto que ele prova."""
        from apps.scrapers.content_ranking import _coupon_candidates

        user = get_user_model().objects.create_user("ranking-termo", password="x")
        fonte = FonteIngestao.objects.create(
            slug="ranking-termo-fonte", marketplace="mercadolivre", nome="Cupons")
        config = ConfiguracaoEnvio.objects.create(
            owner=user, grupo_id="teste@g.us", grupo_nome="Teste ofertas",
            canal="whatsapp", macro_categoria="Eletrodomesticos",
            termo_busca="air fryer", min_desconto_percent=15,
        )
        cupom = CupomNormalizado.objects.create(
            fonte=fonte, external_id="ranking-termo-20", marketplace="mercadolivre",
            categoria="Campanhas gerais", titulo="Cupom da loja", codigo="AIR20",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 20}, estado="ativo",
            ultima_observacao=timezone.now(),
        )
        produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Air fryer comprovada", origem="cupom",
            macro_categoria="Eletrodomesticos", preco_sem_desconto=300,
            preco_com_cupom=240, link_produto="https://produto.example/airfryer",
            imagem_url="https://img.example/airfryer.jpg",
        )
        ProdutoCupom.objects.create(
            produto=produto, cupom=cupom, status="confirmado",
            preco_original=300, preco_atual=300, preco_final=240,
            verificado_em=timezone.now(),
        )
        CupomDisponibilidade.objects.create(
            organization=user.perfil.organization, usuario=user, cupom=cupom,
            channel="whatsapp", stage="ready", use_mode="code_notice",
        )

        candidatos = _coupon_candidates(config, limit=5)

        self.assertEqual([item.obj.pk for item in candidatos], [cupom.pk])


class CouponCanaryCommandTests(TestCase):
    def test_preview_usa_par_pronto_quando_projecao_ainda_nao_atualizou(self):
        """O canário homologa o que pode ser enviado, não uma projeção atrasada."""
        from apps.scrapers.coupon_products import chave_produtos_cupom

        user = get_user_model().objects.create_user("canary-pair", password="x")
        config = ConfiguracaoEnvio.objects.create(
            owner=user, grupo_id="teste@g.us", grupo_nome="Teste ofertas",
            canal="whatsapp", macro_categoria="Eletrodomésticos",
            min_desconto_percent=15,
        )
        fonte = FonteIngestao.objects.create(
            slug="canary-pair-source", marketplace="mercadolivre", nome="ML",
        )
        cupom = CupomNormalizado.objects.create(
            fonte=fonte, external_id="canary-pair-20", marketplace="mercadolivre",
            titulo="Cupom de teste", codigo="PAR20", estado="ativo",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 20}, ultima_observacao=timezone.now(),
        )
        produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Air fryer pareada", origem="cupom",
            macro_categoria="Eletrodomésticos", preco_sem_desconto=300,
            preco_com_cupom=240, link_produto="https://produto.example/airfryer",
            imagem_url="https://img.example/airfryer.jpg",
        )
        relacao = ProdutoCupom.objects.create(
            produto=produto, cupom=cupom, status="confirmado",
            preco_original=300, preco_atual=300, preco_final=240,
            verificado_em=timezone.now(),
        )
        CupomPreparacao.objects.create(
            cupom=cupom, usuario=None, status="pronto",
            produtos_chave=chave_produtos_cupom(cupom),
            verificado_em=timezone.now(),
        )
        LinkAfiliadoProdutoCupomUsuario.objects.create(
            usuario=user, relacao=relacao, estado="pronto", verificado_ok=True,
            verificado_em=timezone.now(),
            link_afiliado="https://meli.la/canary-pair",
            url_canonica="https://meli.la/canary-pair",
        )
        saida = StringIO()
        with patch("apps.scrapers.content_ranking._coupon_candidates", return_value=[]):
            call_command("canario_cupom", config=config.pk, username=user.username,
                         stdout=saida)

        self.assertIn(f"cupom={cupom.pk}", saida.getvalue())
        self.assertIn("Prévia somente", saida.getvalue())

    def test_preview_never_envia_sem_flag_explicita(self):
        user = get_user_model().objects.create_user("canary-lu", password="x")
        config = ConfiguracaoEnvio.objects.create(
            owner=user, grupo_id="teste@g.us", grupo_nome="Teste ofertas",
            canal="whatsapp", min_desconto_percent=15,
        )
        cupom = SimpleNamespace(pk=10)
        produto = SimpleNamespace(pk=20, nome="Produto comprovado")
        relacao = SimpleNamespace(
            produto=produto, preco_atual=100, preco_final=80,
        )
        candidato = SimpleNamespace(
            kind="coupon", obj=cupom, score=60, reasons=["20% de desconto"],
        )
        saida = StringIO()
        with patch(
            "apps.scrapers.content_ranking._coupon_candidates",
            return_value=[candidato],
        ), patch(
            "apps.scrapers.coupon_products.relacoes_prontas_para_envio",
            return_value=[relacao],
        ), patch(
            "apps.scrapers.ofertas._desconto_efetivo_do_item_cupom", return_value=20,
        ), patch(
            "apps.scrapers.ofertas._piso_desconto_cupom", return_value=15,
        ), patch("apps.scrapers.ofertas.enviar_cupom") as enviar:
            call_command("canario_cupom", config=config.pk, username=user.username,
                         stdout=saida)

        self.assertIn("Prévia somente", saida.getvalue())
        enviar.assert_not_called()

    def test_preview_pula_registro_ready_sem_relacao_e_acha_o_proximo(self):
        user = get_user_model().objects.create_user("canary-next", password="x")
        config = ConfiguracaoEnvio.objects.create(
            owner=user, grupo_id="teste@g.us", grupo_nome="Teste ofertas",
            canal="whatsapp", min_desconto_percent=15,
        )
        invalido = SimpleNamespace(pk=10)
        valido = SimpleNamespace(pk=11)
        produto = SimpleNamespace(pk=20, nome="Produto comprovado")
        relacao = SimpleNamespace(
            produto=produto, preco_atual=100, preco_final=80,
        )
        candidatos = [
            SimpleNamespace(kind="coupon", obj=invalido, score=80, reasons=[]),
            SimpleNamespace(kind="coupon", obj=valido, score=60, reasons=[]),
        ]
        saida = StringIO()
        with patch(
            "apps.scrapers.content_ranking._coupon_candidates", return_value=candidatos,
        ), patch(
            "apps.scrapers.coupon_products.relacoes_prontas_para_envio",
            side_effect=lambda cupom, _user: [] if cupom is invalido else [relacao],
        ), patch(
            "apps.scrapers.ofertas._desconto_efetivo_do_item_cupom", return_value=20,
        ), patch(
            "apps.scrapers.ofertas._piso_desconto_cupom", return_value=15,
        ):
            call_command("canario_cupom", config=config.pk, username=user.username,
                         stdout=saida)

        self.assertIn("cupom=11", saida.getvalue())


class AmazonPublicCouponsCadenceTests(TestCase):
    @patch("apps.scrapers.report_sessions.has_report_session", return_value=True)
    @patch("apps.scrapers.sources.run_source")
    def test_shopee_oficial_usa_sessao_conectada_e_escopo_do_usuario(
        self, run_source, _has_session,
    ):
        from apps.scrapers.coupon_pipeline import coletar_cupons

        user = get_user_model().objects.create_user("pipe-shopee", password="x")
        run_source.return_value = {
            "status": "empty", "offers": [], "coupons": [],
        }

        coletar_cupons(usuarios=[user], incluir_awin=False)

        shopee_calls = [
            call for call in run_source.call_args_list
            if call.args[0] == "shopee-public-coupons"
        ]
        self.assertEqual(len(shopee_calls), 1)
        self.assertEqual(shopee_calls[0].kwargs["usuario"], user)

    def test_catalogo_fresco_pula_chromium(self):
        from apps.scrapers.coupon_pipeline import coletar_cupons

        get_user_model().objects.create_user("pipe-az", password="x")
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="amazon-public-coupons",
            defaults={"marketplace": "amazon", "nome": "Amazon cupons"},
        )
        fonte.ultimo_sucesso = timezone.now()
        fonte.save(update_fields=["ultimo_sucesso"])
        CupomNormalizado.objects.create(
            fonte=fonte, external_id="az-1", marketplace="amazon",
            titulo="Cupom Amazon", estado="ativo",
            regras={"modo_resgate": "ativacao"},
        )
        with patch(
            "apps.scrapers.sources.run_source",
            return_value={"status": "empty", "offers": [], "coupons": []},
        ) as run:
            result = coletar_cupons(incluir_awin=False)
        slugs = [call.args[0] for call in run.call_args_list]
        self.assertNotIn("amazon-public-coupons", slugs)
        self.assertEqual(result["fontes"]["amazon-public-coupons"]["status"], "skipped")

    def test_catalogo_velho_coleta_de_novo(self):
        from apps.scrapers.coupon_pipeline import coletar_cupons

        get_user_model().objects.create_user("pipe-az-old", password="x")
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="amazon-public-coupons",
            defaults={"marketplace": "amazon", "nome": "Amazon cupons"},
        )
        fonte.ultimo_sucesso = timezone.now() - timedelta(hours=7)
        fonte.save(update_fields=["ultimo_sucesso"])
        CupomNormalizado.objects.create(
            fonte=fonte, external_id="az-old", marketplace="amazon",
            titulo="Cupom velho", estado="ativo",
            regras={"modo_resgate": "ativacao"},
        )
        with patch(
            "apps.scrapers.sources.run_source",
            return_value={"status": "empty", "offers": [], "coupons": []},
        ) as run:
            coletar_cupons(incluir_awin=False)
        slugs = [call.args[0] for call in run.call_args_list]
        self.assertIn("amazon-public-coupons", slugs)
