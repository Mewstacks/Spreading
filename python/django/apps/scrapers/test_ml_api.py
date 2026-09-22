"""A porta do ML que não depende de navegador: API oficial, sem login."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.scrapers import ml_api, preco_ao_vivo


CREDENCIAIS = {"ML_API_CLIENT_ID": "app-123", "ML_API_CLIENT_SECRET": "segredo"}


def _limpar_token():
    ml_api._TOKEN.update({"valor": "", "expira_em": 0.0, "silencio_ate": 0.0})


@override_settings(**CREDENCIAIS)
class MLApiTokenTests(SimpleTestCase):
    def setUp(self):
        _limpar_token()
        self.addCleanup(_limpar_token)

    def test_token_e_reusado_ate_perto_de_vencer(self):
        with patch.object(ml_api, "_pedir_token", return_value={
            "access_token": "APP_USR-1", "expires_in": 21600,
        }) as pedir:
            self.assertEqual(ml_api.access_token(), "APP_USR-1")
            self.assertEqual(ml_api.access_token(), "APP_USR-1")
        # Uma chamada de token por processo a cada ~6h, não uma por item.
        self.assertEqual(pedir.call_count, 1)

    def test_recusa_recua_em_vez_de_repetir_por_item(self):
        with patch.object(ml_api, "_pedir_token",
                          side_effect=RuntimeError("400 unsupported_grant_type")) as pedir:
            self.assertEqual(ml_api.access_token(), "")
            self.assertEqual(ml_api.access_token(), "")
        self.assertEqual(pedir.call_count, 1)

    def test_primeira_chamada_da_maquina_nao_nasce_em_recuo(self):
        """`monotonic` é o uptime: zero não pode significar "falhou agora"."""
        with patch.object(ml_api.time, "monotonic", return_value=12.0), \
                patch.object(ml_api, "_pedir_token", return_value={
                    "access_token": "APP_USR-1", "expires_in": 21600}) as pedir:
            self.assertEqual(ml_api.access_token(), "APP_USR-1")
        pedir.assert_called_once()

    def test_sem_credencial_de_app_a_fonte_nao_responde(self):
        with override_settings(ML_API_CLIENT_ID="", ML_API_CLIENT_SECRET=""):
            self.assertFalse(ml_api.configurado())
            self.assertEqual(ml_api.access_token(), "")


@override_settings(**CREDENCIAIS)
class MLApiPrecoTests(SimpleTestCase):
    def setUp(self):
        _limpar_token()
        self.addCleanup(_limpar_token)

    def test_catalogo_usa_a_oferta_do_buy_box_e_nao_a_mais_barata(self):
        # O link publicado abre a oferta ganhadora; anunciar a mais barata da
        # lista é prometer um preço que o comprador não encontra.
        with patch.object(ml_api, "_get", return_value={"results": [
            {"item_id": "MLB1", "price": 78.9, "original_price": 104.9,
             "tags": ["kvs_primary"]},
            {"item_id": "MLB2", "price": 39.99, "original_price": 0, "tags": []},
        ]}):
            self.assertEqual(
                ml_api.preco_do_produto("MLB66637233"),
                {"preco": 78.9, "preco_de": 104.9, "item_id": "MLB1"},
            )

    def test_catalogo_sem_marca_de_buy_box_segue_a_ordem_da_api(self):
        with patch.object(ml_api, "_get", return_value={"results": [
            {"item_id": "MLB1", "price": 229.0, "tags": []},
            {"item_id": "MLB2", "price": 199.0, "tags": []},
        ]}):
            self.assertEqual(ml_api.preco_do_produto("MLB1")["preco"], 229.0)

    def test_produto_sem_oferta_nao_vira_preco(self):
        with patch.object(ml_api, "_get", return_value={"results": []}):
            self.assertEqual(ml_api.preco_do_produto("MLB1"), {})

    def test_anuncio_pausado_nao_vira_preco(self):
        with patch.object(ml_api, "_get", return_value={
            "status": "paused", "price": 199.9,
        }):
            self.assertEqual(ml_api.preco_do_item("MLB4555189589"), {})

    def test_link_de_catalogo_mede_pela_api_sem_abrir_a_pagina(self):
        produto = SimpleNamespace(
            marketplace="mercadolivre",
            link_produto="https://www.mercadolivre.com.br/creatina/p/MLB66637233",
        )
        with patch.object(ml_api, "preco_do_produto",
                          return_value={"preco": 78.9, "preco_de": 104.9}), \
                patch.object(preco_ao_vivo, "sessao_ml") as sessao:
            vivo = preco_ao_vivo._preco_ml(produto)
        # A página nem é aberta: é justamente o GET que o anti-bot recusa.
        sessao.assert_not_called()
        self.assertEqual(vivo["preco"], 78.9)
        self.assertEqual(vivo["fonte"], "ml-api-catalogo")

    def test_anuncio_solto_cai_no_caminho_antigo(self):
        # `/items/{id}` responde 403 para vendedor terceiro; o GET com cookies
        # continua sendo a única porta desses links.
        produto = SimpleNamespace(
            marketplace="mercadolivre",
            link_produto="https://produto.mercadolivre.com.br/MLB-3777406449-x",
        )
        with patch.object(ml_api, "preco_do_item", return_value={}), \
                patch.object(preco_ao_vivo, "sessao_ml", return_value=None) as sessao:
            self.assertIsNone(preco_ao_vivo._preco_ml(produto))
        sessao.assert_called_once()
