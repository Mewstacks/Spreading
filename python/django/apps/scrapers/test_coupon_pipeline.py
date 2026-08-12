from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import OperationalError, connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.scrapers.models import (
    Cupom, CupomNormalizado, CupomPreparacao, FonteIngestao,
    LinkAfiliadoUsuario, Produto, ProdutoCupom,
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
                    "apps.scrapers.scraper_mercadolivre.scraper.connections.close_all"
                ) as close, \
                patch("apps.scrapers.scraper_mercadolivre.scraper.time.sleep"):
            total = _persistir_campanhas_cupons(
                rows, varredura_completa=False, pagina_final=1,
            )

        self.assertEqual(total, 1)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(close.call_count, 2)
        self.assertTrue(Cupom.objects.filter(campanha_id="retry-1").exists())


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

        marketplace = SimpleNamespace(prefetch_links=generate)

        def verify(usuario, limite=20, produto_ids=None):
            LinkAfiliadoUsuario.objects.filter(
                usuario=usuario, produto_id__in=produto_ids,
            ).update(verificado_ok=True, verificado_em=timezone.now())
            return {"aprovados": len(produto_ids), "reprovados": 0, "transitorios": 0}

        with patch(
            "apps.scrapers.marketplaces.registry.get_marketplace",
            return_value=marketplace,
        ), patch(
            "apps.scrapers.scraper_mercadolivre.link.verificar_links_pendentes",
            side_effect=verify,
        ) as verifier:
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

        self.assertEqual(result["falhos"], 2)
        self.assertEqual(
            result["fontes"]["ml-cupons-afiliados"]["status"], "error",
        )
        self.assertEqual(
            result["fontes"]["amazon-public-coupons"]["status"], "error",
        )
