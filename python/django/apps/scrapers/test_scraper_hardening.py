from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.scrapers.models import ExecucaoIngestao, FonteIngestao, Produto
from apps.scrapers.scraper_amazon import creators_api
from apps.scrapers.scraper_mercadolivre import link as ml_link


class LinkBuilderHardeningTests(SimpleTestCase):
    def test_rejeicao_brasil_e_terminal(self):
        with self.assertRaises(ml_link.UrlNaoPermitidaError):
            ml_link._validar_resultado_link(
                "⚠️ Este URL não é do Mercado Livre Brasil."
            )

    def test_url_isca_rejeita_host_externo_e_credenciais(self):
        self.assertIsNone(
            ml_link._montar_url_isca("https://example.com/MLB-123456789", "")
        )
        self.assertIsNone(
            ml_link._montar_url_isca(
                "https://usuario:senha@produto.mercadolivre.com.br/MLB-123456789",
                "",
            )
        )

    def test_url_isca_normaliza_http_para_https(self):
        self.assertEqual(
            ml_link._montar_url_isca(
                "http://produto.mercadolivre.com.br/MLB-123456789", "camp"
            ),
            "https://produto.mercadolivre.com.br/MLB-123456789?coupon_campaign_id=camp",
        )

    @patch.object(ml_link, "_registrar_veredito_lb")
    @patch.object(ml_link, "_pagina_de_login", return_value=False)
    @patch.object(ml_link, "_pagina_intersticial", return_value=False)
    def test_formulario_ausente_interrompe_antes_do_lote(
        self, _intersticial, _login, registrar
    ):
        page = Mock()
        usuario = Mock()
        page.get_by_role.return_value.wait_for.side_effect = TimeoutError("layout")

        with self.assertRaisesRegex(ml_link.AuthError, "lote foi preservado"):
            ml_link._abrir_link_builder(page, usuario=usuario)

        registrar.assert_called_with(
            usuario,
            "inconclusivo",
            "formulário do Link Builder indisponível",
        )


class ScraperPersistenceHardeningTests(TestCase):
    def test_busca_vazia_preserva_catalogo(self):
        existente = Produto.objects.create(
            marketplace="mercadolivre", origem="busca", nome="Fone Bluetooth",
            preco_sem_desconto=100, preco_com_cupom=80,
            link_produto="https://produto.mercadolivre.com.br/MLB-123456789",
        )
        page = Mock()
        page.locator.return_value.all.return_value = []

        class Browser:
            def __enter__(self):
                return page, Mock()

            def __exit__(self, *_args):
                return False

        with patch(
            "apps.scrapers.scraper_mercadolivre.ofertas_scraper.iniciar_browser",
            return_value=Browser(),
        ), patch(
            "apps.scrapers.scraper_mercadolivre.ofertas_scraper._coletar_cards",
            return_value=[],
        ), patch(
            "apps.scrapers.scraper_mercadolivre.ofertas_scraper.pausa_humana"
        ):
            from apps.scrapers.scraper_mercadolivre.ofertas_scraper import (
                buscar_por_termo,
            )

            self.assertEqual(buscar_por_termo("fone", max_paginas=1), 0)

        self.assertTrue(Produto.objects.filter(pk=existente.pk).exists())

    def test_reconciliacao_fecha_execucao_abandonada(self):
        fonte = FonteIngestao.objects.create(
            slug="teste-orfa", marketplace="mercadolivre", nome="Teste"
        )
        execucao = ExecucaoIngestao.objects.create(fonte=fonte)
        ExecucaoIngestao.objects.filter(pk=execucao.pk).update(
            iniciada_em=timezone.now() - timedelta(hours=3)
        )

        from apps.scrapers.maintenance import reconciliar_execucoes_ingestao_orfas

        self.assertEqual(reconciliar_execucoes_ingestao_orfas(), 1)
        execucao.refresh_from_db()
        self.assertEqual(execucao.status, "error")
        self.assertIsNotNone(execucao.finalizada_em)

    @patch(
        "apps.scrapers.scraper_mercadolivre.cupons_container._ids_por_http",
        return_value=None,
    )
    @patch(
        "apps.scrapers.scraper_mercadolivre.cupons_container.storage_state",
        return_value=None,
    )
    def test_container_autenticado_sem_sessao_degrada_em_vez_de_falso_sucesso(
        self, _state, _http
    ):
        fonte = FonteIngestao.objects.create(
            slug="ml-container-teste", marketplace="mercadolivre", nome="Teste"
        )
        from apps.scrapers.models import CupomNormalizado

        CupomNormalizado.objects.create(
            fonte=fonte, external_id="container:1", marketplace="mercadolivre",
            titulo="Cupom", estado="ativo",
            regras={
                "container_url": "https://lista.mercadolivre.com.br/_Container_x",
                "is_mar_aberto": False,
            },
        )
        Produto.objects.create(
            marketplace="mercadolivre", origem="oferta", estado="ativo",
            nome="Produto", preco_sem_desconto=100, preco_com_cupom=80,
            link_produto="https://produto.mercadolivre.com.br/MLB-123456789",
        )
        from apps.scrapers.scraper_mercadolivre.cupons_container import (
            SessaoMLObrigatoriaError,
            casar_cupons_container,
        )

        with self.assertRaises(SessaoMLObrigatoriaError):
            casar_cupons_container()


class AmazonCredentialHardeningTests(SimpleTestCase):
    @patch.object(creators_api, "_obter_token", side_effect=creators_api.AmazonNotEligible("401"))
    def test_credencial_rejeitada_nao_e_embrulhada(self, _token):
        creds = creators_api.Credenciais("id", "secret", "host", "tag")
        with self.assertRaises(creators_api.AmazonNotEligible):
            creators_api._post("searchItems", {}, creds)
