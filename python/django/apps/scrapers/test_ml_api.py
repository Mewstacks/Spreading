"""A porta do ML que não depende de navegador: API oficial + token renovável."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scrapers import ml_api, preco_ao_vivo
from apps.scrapers.models import MLApiToken


CREDENCIAIS = {
    "ML_API_CLIENT_ID": "app-123",
    "ML_API_CLIENT_SECRET": "segredo",
    # O token fica cifrado em repouso (EncryptedCharField/Fernet).
    "SECRETS_FERNET_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
}


@override_settings(**CREDENCIAIS)
class MLApiTokenTests(TestCase):
    def setUp(self):
        ml_api._ULTIMA_FALHA["quando"] = 0.0

    def test_token_valido_nao_gasta_o_refresh(self):
        # Refresh é de uso único: renovar sem precisar queima a credencial.
        MLApiToken.objects.create(
            access_token="APP_USR-vivo", refresh_token="TG-1",
            expira_em=timezone.now() + timedelta(hours=5),
        )
        with patch.object(ml_api, "_post_token") as post:
            self.assertEqual(ml_api.access_token(), "APP_USR-vivo")
        post.assert_not_called()

    def test_token_perto_de_vencer_renova_e_guarda_o_novo_refresh(self):
        MLApiToken.objects.create(
            access_token="APP_USR-velho", refresh_token="TG-1",
            expira_em=timezone.now() + timedelta(minutes=2),
        )
        with patch.object(ml_api, "_post_token", return_value={
            "access_token": "APP_USR-novo", "refresh_token": "TG-2",
            "expires_in": 21600, "user_id": 42,
        }) as post:
            self.assertEqual(ml_api.access_token(), "APP_USR-novo")
        self.assertEqual(post.call_args.args[0]["grant_type"], "refresh_token")
        registro = MLApiToken.objects.get()
        # Guardar o refresh NOVO é o ponto: o antigo já morreu no ML.
        self.assertEqual(registro.refresh_token, "TG-2")
        self.assertEqual(registro.conta_id, "42")

    def test_falha_de_credencial_recua_em_vez_de_repetir_por_item(self):
        MLApiToken.objects.create(
            access_token="", refresh_token="TG-queimado",
            expira_em=timezone.now() - timedelta(minutes=1),
        )
        with patch.object(ml_api, "_post_token",
                          side_effect=RuntimeError("400 invalid_grant")) as post:
            self.assertEqual(ml_api.access_token(), "")
            self.assertEqual(ml_api.access_token(), "")
        self.assertEqual(post.call_count, 1)
        self.assertIn("invalid_grant", MLApiToken.objects.get().ultimo_erro)

    def test_sem_credencial_de_app_a_fonte_simplesmente_nao_responde(self):
        with override_settings(ML_API_CLIENT_ID="", ML_API_CLIENT_SECRET=""):
            self.assertFalse(ml_api.configurado())
            self.assertEqual(ml_api.access_token(), "")


@override_settings(**CREDENCIAIS)
class MLApiPrecoTests(TestCase):
    def setUp(self):
        ml_api._ULTIMA_FALHA["quando"] = 0.0

    def test_anuncio_pausado_nao_vira_preco(self):
        with patch.object(ml_api, "item", return_value={
            "status": "paused", "price": 199.9,
        }):
            self.assertEqual(ml_api.preco_do_item("MLB123"), {})

    def test_anuncio_ativo_devolve_preco_e_de(self):
        with patch.object(ml_api, "item", return_value={
            "status": "active", "price": 117.0, "original_price": 199.9,
        }):
            self.assertEqual(ml_api.preco_do_item("MLB123"),
                             {"preco": 117.0, "preco_de": 199.9})

    def test_preco_ml_tenta_a_api_antes_da_pagina(self):
        produto = SimpleNamespace(
            marketplace="mercadolivre",
            link_produto="https://produto.mercadolivre.com.br/MLB-123456789-x",
        )
        with patch.object(ml_api, "preco_do_item",
                          return_value={"preco": 117.0, "preco_de": 199.9}), \
                patch.object(preco_ao_vivo, "sessao_ml") as sessao:
            vivo = preco_ao_vivo._preco_ml(produto)
        # A página nem é aberta: é justamente o GET que o anti-bot recusa.
        sessao.assert_not_called()
        self.assertEqual(vivo["preco"], 117.0)
        self.assertEqual(vivo["fonte"], "ml-api-oficial")

    def test_api_muda_nada_quando_o_app_nao_esta_autorizado(self):
        produto = SimpleNamespace(
            marketplace="mercadolivre",
            link_produto="https://produto.mercadolivre.com.br/MLB-123456789-x",
        )
        with patch.object(ml_api, "preco_do_item", return_value={}), \
                patch.object(preco_ao_vivo, "sessao_ml", return_value=None):
            self.assertIsNone(preco_ao_vivo._preco_ml(produto))
