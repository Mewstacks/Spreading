"""Preço publicado x preço da vitrine.

A página oficial de cupons da Amazon mostra dois números: o da vitrine e o que o
cliente paga depois de ativar o cupom. A mensagem precisa anunciar o segundo —
anunciar o primeiro junto de "ative o cupom" promete um valor que a página não
cobra.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.scrapers.models import Produto
from apps.scrapers.ofertas import _preco_br, montar_mensagem, preco_publicavel
from apps.scrapers.sources.amazon_coupons import _money


class PrecoPublicavelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("preco-user")

    def _produto(self, **campos):
        base = {
            "owner": self.user, "marketplace": "amazon", "asin": "B012345678",
            "nome": "Cafeteira Expresso", "origem": "oferta",
            "link_produto": "https://www.amazon.com.br/dp/B012345678",
            "fonte": "amazon-public-coupons", "estado": "ativo",
            "preco_sem_desconto": 120.0, "preco_com_cupom": 100.0,
            "preco_efetivo": 90.0,
            "evidencia": {"promotion": {"present": True, "coupon_confirmed": True}},
        }
        base.update(campos)
        return Produto.objects.create(**base)

    def test_usa_preco_efetivo_quando_menor_que_a_vitrine(self):
        produto = self._produto()
        self.assertEqual(preco_publicavel(produto), 90.0)

    def test_cai_na_vitrine_quando_nao_ha_preco_efetivo(self):
        self.assertEqual(preco_publicavel(self._produto(preco_efetivo=0)), 100.0)

    def test_ignora_preco_efetivo_incoerente(self):
        """Efetivo maior que a vitrine é dado corrompido, não promoção."""
        self.assertEqual(preco_publicavel(self._produto(preco_efetivo=150.0)), 100.0)

    def test_mensagem_anuncia_o_preco_pago_junto_da_linha_de_cupom(self):
        produto = self._produto()
        texto = montar_mensagem(produto, "https://link", None)

        self.assertIn("POR 90", texto)
        self.assertNotIn("POR 100", texto)
        # O "DE" continua sendo a referência da vitrine riscada.
        self.assertIn("120", texto)
        self.assertIn("CUPOM: ative na página da Amazon", texto)

    def test_produto_sem_cupom_mantem_o_preco_de_vitrine(self):
        produto = self._produto(
            preco_efetivo=0, evidencia={}, fonte="amazon-creators-api",
        )
        texto = montar_mensagem(produto, "https://link", None)
        self.assertIn("POR 100", texto)
        self.assertNotIn("CUPOM", texto)


class NormalizacaoDinheiroTests(TestCase):
    def test_money_aceita_ponto_decimal_e_ponto_de_milhar(self):
        casos = [
            ("R$ 64.99", 64.99),      # ponto como decimal — virava 6499.0
            ("R$ 1.299,90", 1299.90),  # ponto como milhar
            ("R$ 1 299,90", 1299.90),  # espaço como milhar
            ("R$ 1.299", 1299.0),      # milhar sem centavos
            ("R$ 89,90", 89.90),
            ("R$ 12.345.678,90", 12345678.90),
            ("", 0.0),
            ("sem preço", 0.0),
        ]
        for texto, esperado in casos:
            with self.subTest(texto=texto):
                self.assertEqual(_money(texto), esperado)

    def test_preco_br_usa_separador_de_milhar(self):
        casos = [
            (1299.9, "1.299,90"),
            (1299.0, "1.299"),
            (64.99, "64,99"),
            (89.5, "89,50"),
            (999.0, "999"),
            (12345678.9, "12.345.678,90"),
        ]
        for valor, esperado in casos:
            with self.subTest(valor=valor):
                self.assertEqual(_preco_br(valor), esperado)
