"""Cobertura, evidência e apresentação equilibrada das duas lojas.

Cobre os pontos do plano em que Amazon e Mercado Livre precisam ser tratados como
iguais: promoção que não é cupom, catálogo público compartilhado, força da
evidência de uma campanha e presença das duas lojas na primeira página.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.scrapers.models import (
    Cupom, CupomNormalizado, FonteIngestao, Produto,
)


class AmazonPromotionKindTests(TestCase):
    """`dealDetails` serve para três coisas; só uma delas é cupom."""

    def _item(self, rotulo):
        return {
            "asin": "B0KIND0001",
            "itemInfo": {"title": {"displayValue": "Produto"}},
            "offersV2": {"listings": [{
                "price": {"money": {"amount": 80},
                          "savingBasis": {"money": {"amount": 100}}},
                "dealDetails": {"displayName": rotulo},
            }]},
            "images": {"primary": {"large": {"url": "https://e/i.jpg"}}},
        }

    def test_classifica_cupom_relampago_e_desconto(self):
        from apps.scrapers.scraper_amazon.ofertas_scraper import classificar_promocao

        self.assertEqual(classificar_promocao("Cupom de 10%"), "coupon")
        self.assertEqual(classificar_promocao("Coupon"), "coupon")
        self.assertEqual(classificar_promocao("Oferta relâmpago"), "lightning_deal")
        self.assertEqual(classificar_promocao("Lightning Deal"), "lightning_deal")
        self.assertEqual(classificar_promocao("Promoção"), "deal")
        self.assertEqual(classificar_promocao(""), "")

    def test_oferta_relampago_nao_vira_cupom(self):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        mapeado = az._mapear_item(self._item("Oferta relâmpago"))
        self.assertEqual(mapeado["tipo_promocao"], "lightning_deal")
        self.assertFalse(mapeado["cupom_confirmado"])

        user = get_user_model().objects.create_user("kind-relampago")
        az.mapear_cupons_codigo(usuario=user, itens=[mapeado])
        produto = Produto.objects.get(marketplace="amazon", asin="B0KIND0001")
        # Sem cupom não há ativação a anunciar: entra como oferta de preço.
        self.assertEqual(produto.origem, "oferta")
        self.assertFalse(produto.evidencia["promotion"]["coupon_confirmed"])
        self.assertEqual(produto.evidencia["promotion"]["kind"], "lightning_deal")

    def test_cupom_confirmado_mantem_semantica_de_ativacao(self):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        user = get_user_model().objects.create_user("kind-cupom")
        mapeado = az._mapear_item(self._item("Cupom de R$ 20"))
        az.mapear_cupons_codigo(usuario=user, itens=[mapeado])
        produto = Produto.objects.get(marketplace="amazon", asin="B0KIND0001")
        self.assertEqual(produto.origem, "cupom_codigo")
        self.assertTrue(produto.evidencia["promotion"]["coupon_confirmed"])


class AmazonPublicCatalogTests(TestCase):
    """Catálogo público é compartilhado; o que é por usuário é o link."""

    def test_fallback_publico_persiste_uma_vez_para_todos(self):
        from apps.scrapers.marketplaces.amazon import Amazon

        usuarios = [
            get_user_model().objects.create_user(f"publico-{i}") for i in range(3)
        ]
        with patch("apps.scrapers.sources.run_source",
                   return_value={"offers": ["item"]}) as run, \
                patch("apps.scrapers.sources.persistence.persist_items",
                      return_value={"offers": 1, "coupons": 0}) as persist:
            Amazon._scrape_publico(usuarios)

        run.assert_called_once()
        persist.assert_called_once()
        self.assertIsNone(persist.call_args.kwargs["owner"])

    def test_sem_usuario_elegivel_nao_coleta(self):
        from apps.scrapers.marketplaces.amazon import Amazon

        with patch("apps.scrapers.sources.run_source") as run:
            Amazon._scrape_publico([])
        run.assert_not_called()


class AmazonCoverageMetricsTests(TestCase):
    """"6 ofertas" pode ser o catálogo inteiro ou o teto da varredura."""

    @patch("apps.scrapers.scraper_amazon.ofertas_scraper.creators_api.search_items")
    def test_metricas_registram_keywords_paginas_e_teto(self, search_items):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        item = {
            "asin": "B0METRIC01",
            "itemInfo": {"title": {"displayValue": "Produto"}},
            "offersV2": {"listings": [{
                "price": {"money": {"amount": 80},
                          "savingBasis": {"money": {"amount": 100}}},
            }]},
            "images": {"primary": {"large": {"url": "https://e/i.jpg"}}},
        }
        search_items.side_effect = [[item], [item]]
        metricas = az.metricas_vazias()

        az._coletar_termos(["fone"], 10, creds=None, max_paginas=2,
                           metricas=metricas)

        self.assertEqual(metricas["keywords"], 1)
        self.assertEqual(metricas["paginas"], 2)
        self.assertEqual(metricas["chamadas"], 2)
        self.assertEqual(metricas["por_keyword"], {"fone": 2})
        # Leu todas as páginas permitidas: existe catálogo além do teto.
        self.assertEqual(metricas["paginas_no_teto"], 1)

    @patch("apps.scrapers.scraper_amazon.ofertas_scraper.creators_api.search_items")
    def test_erro_de_quota_aparece_no_diagnostico(self, search_items):
        from apps.scrapers.scraper_amazon import ofertas_scraper as az

        search_items.side_effect = RuntimeError("HTTP 429 Too Many Requests")
        metricas = az.metricas_vazias()
        with self.assertRaises(Exception):
            az._coletar_termos(["fone"], 10, creds=None, metricas=metricas)

        self.assertEqual(metricas["erros_por_tipo"], {"Throttled429": 1})


class EvidenceStrengthTests(TestCase):
    """URL sintética não pode valer como container observado."""

    def setUp(self):
        # A fonte já é semeada por migration; get_or_create mantém o teste
        # independente da ordem de execução.
        self.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML"},
        )

    def _cupom(self, link, regras=None, campanha="CAMP1"):
        return CupomNormalizado.objects.create(
            fonte=self.fonte, external_id=f"campanha:{campanha}",
            marketplace="mercadolivre", titulo="Cupom", link=link,
            regras=regras or {"tipo_desconto": "porcentagem", "valor_desconto": 10},
        )

    def test_classifica_as_tres_forcas(self):
        from apps.scrapers.coupon_rules import forca_evidencia

        container = self._cupom(
            "https://lista.mercadolivre.com.br/_Container_marcas",
            regras={"container_url": "https://lista.mercadolivre.com.br/_Container_x",
                    "valor_desconto": 10},
        )
        self.assertEqual(forca_evidencia(container), "official_container")

        loja = self._cupom("https://lista.mercadolivre.com.br/_CustId_12345",
                           campanha="CAMP2")
        self.assertEqual(forca_evidencia(loja), "structured_listing")

        sintetico = self._cupom(
            "https://lista.mercadolivre.com.br/_Container_CAMP3", campanha="CAMP3")
        self.assertEqual(forca_evidencia(sintetico), "synthetic_candidate")

        sem_listagem = self._cupom("https://www.mercadolivre.com.br/cupons",
                                   campanha="CAMP4")
        self.assertEqual(forca_evidencia(sem_listagem), "")

    def test_projecao_grava_a_forca_da_evidencia(self):
        from apps.scrapers.scraper_mercadolivre.scraper import projetar_catalogo_cupons

        Cupom.objects.create(
            campanha_id="PROJ1", titulo="Cupom 10%", tipo_desconto="porcentagem",
            valor_desconto=10, estado="ativo",
            link_original="https://lista.mercadolivre.com.br/_Container_PROJ1",
            ultima_verificacao=timezone.now(),
        )
        projetar_catalogo_cupons()

        cupom = CupomNormalizado.objects.get(external_id="campanha:PROJ1")
        self.assertEqual(cupom.evidencia["evidence_strength"], "synthetic_candidate")

    def test_fila_de_preparo_prefere_container_a_sintetico(self):
        from apps.scrapers.coupon_pipeline import _peso_do_cupom

        container = self._cupom(
            "https://lista.mercadolivre.com.br/_Container_marcas",
            regras={"container_url": "https://lista.mercadolivre.com.br/_Container_x",
                    "valor_desconto": 10},
        )
        sintetico = self._cupom(
            "https://lista.mercadolivre.com.br/_Container_CAMP9", campanha="CAMP9")
        self.assertLess(_peso_do_cupom(container), _peso_do_cupom(sintetico))


class ProjecaoParcialTests(TestCase):
    """Execução parcial não pode renovar campanha que ninguém observou."""

    def test_campanha_nao_observada_nao_e_renovada(self):
        from apps.scrapers.scraper_mercadolivre.scraper import projetar_catalogo_cupons

        agora = timezone.now()
        antiga = Cupom.objects.create(
            campanha_id="ANTIGA", titulo="Cupom antigo", tipo_desconto="porcentagem",
            valor_desconto=10, estado="ativo",
            link_original="https://lista.mercadolivre.com.br/_Container_antiga",
            ultima_verificacao=agora - timezone.timedelta(hours=5),
        )
        Cupom.objects.create(
            campanha_id="NOVA", titulo="Cupom novo", tipo_desconto="porcentagem",
            valor_desconto=20, estado="ativo",
            link_original="https://lista.mercadolivre.com.br/_Container_nova",
            ultima_verificacao=agora,
        )

        projetados = projetar_catalogo_cupons(
            desde=agora - timezone.timedelta(minutes=1))

        self.assertEqual(projetados, 1)
        # A campanha não observada continua no catálogo, apenas não foi "revista".
        self.assertFalse(CupomNormalizado.objects.filter(
            external_id=f"campanha:{antiga.campanha_id}").exists())
        self.assertTrue(CupomNormalizado.objects.filter(
            external_id="campanha:NOVA", estado="ativo").exists())

    def test_projecao_parcial_nao_expira_campanha_vigente(self):
        from apps.scrapers.scraper_mercadolivre.scraper import projetar_catalogo_cupons

        agora = timezone.now()
        for campanha in ("A", "B"):
            Cupom.objects.create(
                campanha_id=campanha, titulo=f"Cupom {campanha}",
                tipo_desconto="porcentagem", valor_desconto=10, estado="ativo",
                link_original=f"https://lista.mercadolivre.com.br/_Container_{campanha}",
                ultima_verificacao=agora,
            )
        projetar_catalogo_cupons()
        self.assertEqual(CupomNormalizado.objects.filter(
            estado="ativo", external_id__startswith="campanha:").count(), 2)

        # Nova execução observa só a campanha A: B continua ativa no catálogo.
        depois = timezone.now()
        Cupom.objects.filter(campanha_id="A").update(ultima_verificacao=depois)
        projetar_catalogo_cupons(desde=depois)

        self.assertEqual(CupomNormalizado.objects.filter(
            estado="ativo", external_id__startswith="campanha:").count(), 2)


class PrimeiraPaginaEquilibradaTests(TestCase):
    """Uma loja inteira não pode ficar invisível por causa do ranking global."""

    def _itens(self, marketplace, quantos, inicio=0):
        return [
            SimpleNamespace(id=inicio + i, marketplace=marketplace)
            for i in range(quantos)
        ]

    def test_amazon_entra_na_primeira_pagina(self):
        from apps.scrapers.vitrine import equilibrar_primeira_pagina

        itens = self._itens("mercadolivre", 40) + self._itens("amazon", 10, 100)
        equilibrado = equilibrar_primeira_pagina(itens, por_pagina=20)

        primeira = equilibrado[:20]
        lojas = {item.marketplace for item in primeira}
        self.assertEqual(lojas, {"mercadolivre", "amazon"})
        # A reserva é de até 25% da página, e nada é descartado.
        amazon = [i for i in primeira if i.marketplace == "amazon"]
        self.assertEqual(len(amazon), 5)
        self.assertEqual(len(equilibrado), len(itens))
        self.assertEqual({id(i) for i in equilibrado}, {id(i) for i in itens})

    def test_ordem_dentro_da_loja_e_preservada(self):
        from apps.scrapers.vitrine import equilibrar_primeira_pagina

        itens = self._itens("mercadolivre", 40) + self._itens("amazon", 10, 100)
        equilibrado = equilibrar_primeira_pagina(itens, por_pagina=20)

        for loja in ("mercadolivre", "amazon"):
            ids = [i.id for i in equilibrado if i.marketplace == loja]
            self.assertEqual(ids, sorted(ids))

    def test_loja_unica_fica_intacta(self):
        from apps.scrapers.vitrine import equilibrar_primeira_pagina

        itens = self._itens("mercadolivre", 40)
        self.assertEqual(equilibrar_primeira_pagina(itens, por_pagina=20), itens)

    def test_loja_ja_presente_nao_e_promovida(self):
        from apps.scrapers.vitrine import equilibrar_primeira_pagina

        itens = (self._itens("mercadolivre", 10)
                 + self._itens("amazon", 10, 100)
                 + self._itens("mercadolivre", 30, 200))
        self.assertEqual(equilibrar_primeira_pagina(itens, por_pagina=20), itens)

    def test_contadores_saem_da_mesma_lista(self):
        from apps.scrapers.vitrine import contar_por_marketplace

        itens = self._itens("mercadolivre", 3) + self._itens("amazon", 2, 100)
        self.assertEqual(contar_por_marketplace(itens),
                         {"mercadolivre": 3, "amazon": 2})
