"""Política de revalidação de preço antes do envio.

Aqui é onde um erro silencioso custa dinheiro: ou a mensagem anuncia preço
errado, ou o envio é bloqueado sem necessidade.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.scrapers import preco_ao_vivo
from apps.scrapers.models import ConfiguracaoEnvio, PrecoHistorico, Produto


def _item_api(preco, preco_de):
    """Resposta da Creators API no formato que _mapear_item entende."""
    return [{
        "asin": "B012345678",
        "itemInfo": {"title": {"displayValue": "Cafeteira Expresso"}},
        "offersV2": {"listings": [{
            "price": {
                "money": {"amount": preco},
                "savingBasis": {"money": {"amount": preco_de}},
                "savings": {"percentage": round((preco_de - preco) / preco_de * 100)},
            },
        }]},
    }]


class RevalidacaoTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("preco-vivo")
        self.produto = Produto.objects.create(
            owner=self.user, marketplace="amazon", asin="B012345678",
            nome="Cafeteira Expresso", origem="oferta", estado="ativo",
            link_produto="https://www.amazon.com.br/dp/B012345678",
            preco_sem_desconto=200.0, preco_com_cupom=100.0, preco_efetivo=100.0,
            frase_llm="TÍTULO ANTIGO POR 100", nome_llm="Cafeteira",
        )

    def _revalidar(self, itens, configuracao=None):
        with patch("apps.scrapers.scraper_amazon.creators_api.get_items",
                   return_value=itens), \
             patch("apps.scrapers.scraper_amazon.creators_api.creds_de_usuario",
                   return_value=object()):
            return preco_ao_vivo.revalidar(
                self.produto, usuario=self.user, configuracao=configuracao)

    def test_variacao_dentro_da_tolerancia_nao_grava_nada(self):
        resultado = self._revalidar(_item_api(100.2, 200.0))

        self.assertTrue(resultado["ok"])
        self.assertFalse(resultado["mudou"])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 100.0)
        # Texto da IA preservado: o preço não mudou.
        self.assertEqual(self.produto.frase_llm, "TÍTULO ANTIGO POR 100")

    def test_preco_que_caiu_atualiza_e_segue(self):
        resultado = self._revalidar(_item_api(80.0, 200.0))

        self.assertTrue(resultado["ok"])
        self.assertTrue(resultado["mudou"])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 80.0)
        self.assertEqual(self.produto.preco_efetivo, 80.0)

    def test_preco_que_subiu_mantendo_desconto_segue(self):
        config = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="123@g.us", min_desconto_percent=15.0)
        # 150 de 200 = 25% de desconto, acima do mínimo.
        resultado = self._revalidar(_item_api(150.0, 200.0), configuracao=config)

        self.assertTrue(resultado["ok"])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 150.0)

    def test_preco_que_subiu_derrubando_o_desconto_aborta(self):
        config = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="123@g.us", min_desconto_percent=15.0)
        # 195 de 200 = 2,5%, abaixo do mínimo -> não faz sentido publicar.
        resultado = self._revalidar(_item_api(195.0, 200.0), configuracao=config)

        self.assertFalse(resultado["ok"])
        self.assertIn("desconto caiu", resultado["motivo"])
        # Mesmo abortando, o preço fresco fica gravado para a próxima seleção.
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 195.0)

    def test_mudanca_de_preco_invalida_o_texto_da_ia(self):
        self._revalidar(_item_api(80.0, 200.0))
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.frase_llm, "")
        self.assertEqual(self.produto.nome_llm, "")

    def test_api_indisponivel_e_inconclusivo_e_nao_bloqueia(self):
        with patch("apps.scrapers.scraper_amazon.creators_api.get_items",
                   side_effect=RuntimeError("503")), \
             patch("apps.scrapers.scraper_amazon.creators_api.creds_de_usuario",
                   return_value=object()):
            resultado = preco_ao_vivo.revalidar(self.produto, usuario=self.user)

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["fonte"], "inconclusivo")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 100.0)

    def test_mudanca_registra_historico_de_preco(self):
        self._revalidar(_item_api(80.0, 200.0))
        self.assertTrue(
            PrecoHistorico.objects.filter(marketplace="amazon", preco=80.0).exists())

    def test_mercado_livre_nao_e_revalidado_aqui(self):
        self.produto.marketplace = "mercadolivre"
        self.produto.save(update_fields=["marketplace"])
        resultado = preco_ao_vivo.revalidar(self.produto, usuario=self.user)
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["fonte"], "nao_suportado")
