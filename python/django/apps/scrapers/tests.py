import json
import os
import re
import tempfile
import uuid
from types import SimpleNamespace
from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.scrapers import ofertas, whatsapp_client
from apps.scrapers.afiliado import tag_ml
from apps.scrapers.maintenance import reconciliar_publicacoes_orfas
from apps.scrapers.management.commands.automacao import _rodar_links
from apps.scrapers.marketplaces.registry import get_marketplace
from apps.scrapers.monitor_conexao import wa_conectado
from apps.scrapers.models import (
    CliquePublicacao, ConfiguracaoEnvio, Cupom, CupomNormalizado, FonteIngestao,
    HistoricoEnvio, LinkAfiliadoUsuario, Produto, EventoOperacional, Publicacao,
    ReceitaAfiliado, RelatorioSync,
)
from apps.scrapers.precos import registrar as registrar_preco
from apps.scrapers.scraper_amazon import link as amazon_link
from apps.scrapers.scraper_amazon import ofertas_scraper as amazon_ofertas
from apps.scrapers.scraper_mercadolivre.scraper import _sincronizar_produtos_no_banco
from apps.scrapers.scraper_mercadolivre import link as ml_link


class AutomationStatusSecurityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("status-user", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)

    @patch("apps.scrapers.automacao_state.worker_alive", return_value=True)
    @patch("apps.scrapers.automacao_state.is_enabled", return_value=True)
    @patch("apps.scrapers.automacao_state.read_state")
    def test_status_never_exposes_worker_traceback(self, read_state, _enabled, _alive):
        read_state.return_value = {
            "fase": "aguardando",
            "erro": 'File "/usr/local/lib/python3.12/site-packages/psycopg/connection.py"\nOperationalError: the connection is closed',
        }

        response = self.client.get(reverse("scraper-automacao"), {"tipo": "scrape"})

        self.assertEqual(response.status_code, 200)
        error = response.json()["estado"]["erro"]
        self.assertIn("Falha temporária", error)
        self.assertNotIn("psycopg", error)
        self.assertNotIn("/usr/local", error)

    @patch("apps.scrapers.automacao_state.worker_alive", return_value=False)
    @patch("apps.scrapers.automacao_state.is_enabled", return_value=True)
    @patch("apps.scrapers.automacao_state.read_state", return_value={"fase": "aguardando"})
    def test_enabled_flag_does_not_claim_worker_is_running(self, _state, _enabled, _alive):
        response = self.client.get(reverse("scraper-automacao"), {"tipo": "scrape"})
        data = response.json()
        self.assertTrue(data["habilitada"])
        self.assertFalse(data["worker_vivo"])
        self.assertFalse(data["rodando"])
        self.assertFalse(data["saudavel"])


class AffiliateIdentityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("affiliate", password="test")
        self.product = Produto.objects.create(
            nome="Produto teste",
            preco_sem_desconto=100,
            preco_com_cupom=80,
            link_produto="https://produto.mercadolivre.com.br/MLB-123456789",
            origem="oferta",
        )

    @override_settings(AFILIADO_TAG="global-que-nao-deve-ser-usada")
    def test_ml_does_not_use_manual_or_global_tag(self):
        self.user.perfil.afiliado_tag_ml = "manual-que-nao-deve-ser-usada"
        self.assertEqual(tag_ml(self.user), "")

    def test_ml_link_uses_only_the_users_auth_file(self):
        with tempfile.TemporaryDirectory() as auth_dir:
            user_auth = os.path.join(auth_dir, f"auth_{self.user.id}.json")
            with open(user_auth, "w", encoding="utf-8") as auth_file:
                auth_file.write("{}")

            with (
                override_settings(ML_AUTH_DIR=auth_dir),
                patch.object(
                    ml_link,
                    "afiliate_link_builder",
                    return_value="https://meli.la/user-link",
                ) as builder,
                patch("apps.scrapers.afiliado.salvar_cache") as save_cache,
            ):
                result = ml_link.gerar_link_afiliado_para_produto(
                    self.product, usuario=self.user
                )

            self.assertEqual(result["link_afiliado"], "https://meli.la/user-link")
            self.assertEqual(builder.call_args.kwargs["auth_path"], user_auth)
            save_cache.assert_called_once()

    def test_ml_link_reuses_user_cache_without_session_or_link_builder(self):
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user,
            produto=self.product,
            link_afiliado="https://meli.la/link-cacheado",
            url_isca=self.product.link_produto,
            afiliado_ok=True,
            estado="pronto",
        )

        with tempfile.TemporaryDirectory() as auth_dir, \
             override_settings(ML_AUTH_DIR=auth_dir), \
             patch.object(
                 ml_link,
                 "afiliate_link_builder",
                 side_effect=AssertionError("Link Builder não deveria abrir"),
             ) as builder:
            result = ml_link.gerar_link_afiliado_para_produto(
                self.product, usuario=self.user)

        self.assertEqual(result["link_afiliado"], "https://meli.la/link-cacheado")
        self.assertTrue(result["afiliado_ok"])
        builder.assert_not_called()

    def test_ml_link_never_falls_back_to_global_auth_for_a_user(self):
        with tempfile.TemporaryDirectory() as auth_dir:
            with open(os.path.join(auth_dir, "auth.json"), "w", encoding="utf-8") as auth:
                auth.write("{}")
            with override_settings(ML_AUTH_DIR=auth_dir):
                with self.assertRaises(ml_link.LoginError):
                    ml_link.gerar_link_afiliado_para_produto(
                        self.product, usuario=self.user
                    )

    def test_ml_link_never_falls_back_to_another_users_auth(self):
        """O fallback de ml_auth_path é só p/ job sem usuário: com usuário, nunca."""
        with tempfile.TemporaryDirectory() as auth_dir:
            outro = os.path.join(auth_dir, f"auth_{self.user.id + 1}.json")
            with open(outro, "w", encoding="utf-8") as auth:
                auth.write("{}")
            with override_settings(ML_AUTH_DIR=auth_dir):
                with self.assertRaises(ml_link.LoginError):
                    ml_link.gerar_link_afiliado_para_produto(
                        self.product, usuario=self.user
                    )

    @override_settings(
        AMAZON_PARTNER_TAG="global-20",
        AMAZON_CREATOR_CREDENTIAL_ID="global-id",
        AMAZON_CREATOR_CREDENTIAL_SECRET="global-secret",
        TELEGRAM_BOT_TOKEN="global-token",
    )
    def test_user_integrations_never_inherit_global_credentials(self):
        from apps.scrapers.afiliado import tag_amazon
        from apps.scrapers.scraper_amazon.creators_api import creds_de_usuario
        from apps.scrapers.senders.telegram import resolver_token

        credentials = creds_de_usuario(self.user)
        self.assertEqual(tag_amazon(self.user), "")
        self.assertEqual(credentials.credential_id, "")
        self.assertEqual(credentials.credential_secret, "")
        self.assertEqual(credentials.partner_tag, "")
        self.assertEqual(resolver_token(self.user), "")

    @patch("apps.scrapers.senders.whatsapp.whatsapp_client.enviar_oferta")
    def test_whatsapp_sender_derives_the_users_session(self, enviar):
        enviar.return_value = {"sucesso": True}
        from apps.scrapers.senders.whatsapp import WhatsAppSender

        result = WhatsAppSender().enviar_oferta(
            "123@g.us", "mensagem", usuario=self.user)

        self.assertTrue(result["sucesso"])
        self.assertEqual(enviar.call_args.kwargs["session"], str(self.user.id))


class MLAuthPathTests(SimpleTestCase):
    """Resolução da sessão do ML.

    O bug que originou estes testes: a tela de conexão gravava auth_{id}.json e a
    geração de links lia um auth.json hardcoded que nunca existiu. O Playwright
    subia sem cookies, o ML mandava pro login, e o usuário via "Sessão ML
    expirada" com a sessão perfeitamente viva.
    """

    def _tocar(self, caminho, quando=None):
        with open(caminho, "w", encoding="utf-8") as arquivo:
            arquivo.write("{}")
        if quando is not None:
            os.utime(caminho, (quando, quando))
        return caminho

    def test_usuario_recebe_o_proprio_arquivo(self):
        from apps.scrapers.session_paths import ml_auth_path

        with tempfile.TemporaryDirectory() as d, override_settings(ML_AUTH_DIR=d):
            user = Mock(id=7)
            self.assertEqual(ml_auth_path(user), os.path.join(d, "auth_7.json"))

    def test_usuario_ignora_o_auth_global_e_o_de_terceiros(self):
        from apps.scrapers.session_paths import ml_auth_path

        with tempfile.TemporaryDirectory() as d, override_settings(ML_AUTH_DIR=d):
            self._tocar(os.path.join(d, "auth.json"))
            self._tocar(os.path.join(d, "auth_99.json"))
            self.assertEqual(ml_auth_path(Mock(id=7)), os.path.join(d, "auth_7.json"))

    def test_job_sem_usuario_prefere_o_auth_global_legado(self):
        from apps.scrapers.session_paths import ml_auth_path

        with tempfile.TemporaryDirectory() as d, override_settings(ML_AUTH_DIR=d):
            self._tocar(os.path.join(d, "auth.json"))
            self._tocar(os.path.join(d, "auth_7.json"))
            self.assertEqual(ml_auth_path(), os.path.join(d, "auth.json"))

    def test_job_sem_usuario_cai_na_sessao_mais_recente(self):
        """O que conserta cron/cupons: sem auth.json, usar a sessão real que existe."""
        from apps.scrapers.session_paths import ml_auth_path

        with tempfile.TemporaryDirectory() as d, override_settings(ML_AUTH_DIR=d):
            self._tocar(os.path.join(d, "auth_1.json"), quando=1_000_000)
            self._tocar(os.path.join(d, "auth_2.json"), quando=2_000_000)
            self.assertEqual(ml_auth_path(), os.path.join(d, "auth_2.json"))

    def test_sem_nenhuma_sessao_devolve_o_caminho_legado(self):
        """Não estoura aqui: quem chama reporta 'reconecte' com a mensagem certa."""
        from apps.scrapers.session_paths import ml_auth_path

        with tempfile.TemporaryDirectory() as d, override_settings(ML_AUTH_DIR=d):
            caminho = ml_auth_path()
            self.assertEqual(caminho, os.path.join(d, "auth.json"))
            self.assertFalse(os.path.exists(caminho))

    def test_arquivo_alheio_no_diretorio_nao_vira_sessao(self):
        from apps.scrapers.session_paths import ml_auth_path

        with tempfile.TemporaryDirectory() as d, override_settings(ML_AUTH_DIR=d):
            self._tocar(os.path.join(d, "auth_1.json.bak"))
            self._tocar(os.path.join(d, "outra_coisa.json"))
            self.assertEqual(ml_auth_path(), os.path.join(d, "auth.json"))


class SondaSessaoMLTests(SimpleTestCase):
    """A sonda pergunta ao ML se a sessão salva ainda vale.

    A regra anterior era só a idade do arquivo (mtime <= 7 dias), o que mentia: um
    cookie revogado pelo ML seguia "conectado" por uma semana, enquanto o sync de
    relatório falhava e a Saúde abria incidente ao lado de um dashboard verde.
    """

    def _auth(self, d, nome="auth_7.json", cookies=True):
        caminho = os.path.join(d, nome)
        estado = {"cookies": [{"name": "ssid", "value": "x"}] if cookies else [],
                  "origins": []}
        with open(caminho, "w", encoding="utf-8") as f:
            json.dump(estado, f)
        return caminho

    def test_200_e_sessao_viva(self):
        from apps.scrapers.conexoes import sondar_sessao_ml

        with tempfile.TemporaryDirectory() as d:
            caminho = self._auth(d)
            with patch("apps.scrapers.conexoes.requests.get",
                       return_value=Mock(status_code=200, headers={})):
                self.assertEqual(sondar_sessao_ml(caminho), ("conectado", ""))

    def test_redirect_para_login_e_sessao_expirada(self):
        from apps.scrapers.conexoes import sondar_sessao_ml

        with tempfile.TemporaryDirectory() as d:
            caminho = self._auth(d)
            resposta = Mock(status_code=302,
                            headers={"Location": "https://www.mercadolivre.com.br/jms/mlb/lgz/login"})
            with patch("apps.scrapers.conexoes.requests.get", return_value=resposta):
                veredito, _ = sondar_sessao_ml(caminho)
        self.assertEqual(veredito, "expirado")

    def test_timeout_e_inconclusivo_nao_expirado(self):
        """Oscilação de rede não é logout — a lição de auxiliar.py:85-89."""
        from apps.scrapers.conexoes import sondar_sessao_ml

        with tempfile.TemporaryDirectory() as d:
            caminho = self._auth(d)
            with patch("apps.scrapers.conexoes.requests.get",
                       side_effect=requests.Timeout("estourou")):
                veredito, _ = sondar_sessao_ml(caminho)
        self.assertEqual(veredito, "inconclusivo")

    def test_erro_do_ml_e_inconclusivo(self):
        """5xx é problema do ML, não da sessão: não pode desconectar o usuário."""
        from apps.scrapers.conexoes import sondar_sessao_ml

        with tempfile.TemporaryDirectory() as d:
            caminho = self._auth(d)
            with patch("apps.scrapers.conexoes.requests.get",
                       return_value=Mock(status_code=503, headers={})):
                veredito, _ = sondar_sessao_ml(caminho)
        self.assertEqual(veredito, "inconclusivo")


class EstadoMLTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def _auth(self, d, nome="auth_7.json"):
        caminho = os.path.join(d, nome)
        with open(caminho, "w", encoding="utf-8") as f:
            json.dump({"cookies": [{"name": "ssid", "value": "x"}], "origins": []}, f)
        return caminho

    def test_sem_arquivo_e_desconectado_com_motivo(self):
        from apps.scrapers.conexoes import estado_ml

        with tempfile.TemporaryDirectory() as d, override_settings(ML_AUTH_DIR=d):
            est = estado_ml(Mock(id=7))
        self.assertFalse(est.conectado)
        self.assertEqual(est.detalhe, "sem_sessao")
        self.assertTrue(est.motivo)

    def test_sessao_expirada_apaga_o_arquivo(self):
        """Confirmado o logout, some com a sessão morta: a tela passa a oferecer
        'Reconectar' em vez de insistir que está tudo bem."""
        from apps.scrapers.conexoes import estado_ml

        with tempfile.TemporaryDirectory() as d, override_settings(ML_AUTH_DIR=d):
            caminho = self._auth(d)
            with patch("apps.scrapers.conexoes.sondar_sessao_ml",
                       return_value=("expirado", "redirect")):
                est = estado_ml(Mock(id=7))
            self.assertFalse(os.path.exists(caminho))
        self.assertEqual(est.detalhe, "expirado")

    def test_inconclusivo_preserva_o_ultimo_estado_e_nao_apaga(self):
        from apps.scrapers.conexoes import estado_ml

        with tempfile.TemporaryDirectory() as d, override_settings(ML_AUTH_DIR=d):
            caminho = self._auth(d)
            user = Mock(id=7)
            with patch("apps.scrapers.conexoes.sondar_sessao_ml",
                       return_value=("conectado", "")):
                self.assertTrue(estado_ml(user).conectado)      # popula o cache
            with patch("apps.scrapers.conexoes.sondar_sessao_ml",
                       return_value=("inconclusivo", "timeout")):
                est = estado_ml(user, usar_cache=False)
            self.assertTrue(os.path.exists(caminho))            # não apagou
        self.assertTrue(est.conectado)                          # manteve o que sabia

    def test_conectado_e_cacheado(self):
        """A sonda vai à rede; dashboard e Saúde fazem polling. Sem cache, cada aba
        aberta viraria uma ida ao ML."""
        from apps.scrapers.conexoes import estado_ml

        with tempfile.TemporaryDirectory() as d, override_settings(ML_AUTH_DIR=d):
            self._auth(d)
            user = Mock(id=7)
            with patch("apps.scrapers.conexoes.sondar_sessao_ml",
                       return_value=("conectado", "")) as sonda:
                estado_ml(user)
                estado_ml(user)
                estado_ml(user)
        self.assertEqual(sonda.call_count, 1)


@override_settings(
    WHATSAPP_API_URL="http://whatsapp.internal:3000",
    WHATSAPP_API_KEY="secret",
)
class WhatsAppStatusCacheTests(SimpleTestCase):
    """O status do WhatsApp é cacheado por poucos segundos.

    Sem isso, cada aba com o painel aberto batia no Node a cada poll; com o Node
    fora do ar cada request levava até 10s (timeout 5 × 2 tentativas) segurando uma
    thread do gunicorn, e poucas abas travavam o app inteiro.
    """

    def setUp(self):
        # LocMemCache sobrevive entre testes do mesmo processo: sem isto, a ordem
        # de execução decidiria o resultado.
        cache.clear()
        self.addCleanup(cache.clear)

    @patch("apps.scrapers.whatsapp_client.requests.request")
    def test_status_repetido_bate_uma_vez_so_no_node(self, request):
        request.return_value = Mock(json=lambda: {"conectado": True})

        for _ in range(5):
            self.assertTrue(whatsapp_client.status("user-1")["conectado"])

        self.assertEqual(request.call_count, 1)

    @patch("apps.scrapers.whatsapp_client.requests.request")
    def test_cache_e_por_sessao(self, request):
        request.side_effect = [
            Mock(json=lambda: {"conectado": True}),
            Mock(json=lambda: {"conectado": False}),
        ]

        self.assertTrue(whatsapp_client.status("user-1")["conectado"])
        self.assertFalse(whatsapp_client.status("user-2")["conectado"])
        self.assertTrue(whatsapp_client.status("user-1")["conectado"])

        self.assertEqual(request.call_count, 2)

    @patch("apps.scrapers.whatsapp_client.requests.request")
    def test_node_fora_do_ar_nao_e_remartelado(self, request):
        request.side_effect = requests.ConnectionError("recusou")

        for _ in range(3):
            self.assertFalse(whatsapp_client.status("user-1")["conectado"])

        # 2 tentativas do retry interno, uma vez só — as chamadas seguintes leem
        # o erro cacheado em vez de esperar o timeout de novo.
        self.assertEqual(request.call_count, 2)

    @patch("apps.scrapers.whatsapp_client.requests.request")
    def test_mexer_no_pareamento_invalida_o_cache(self, request):
        request.return_value = Mock(json=lambda: {"conectado": False})
        whatsapp_client.status("user-1")

        request.return_value = Mock(json=lambda: {"conectado": True})
        whatsapp_client.iniciar_sessao("user-1")

        self.assertTrue(whatsapp_client.status("user-1")["conectado"])

    @patch("apps.scrapers.whatsapp_client.requests.request")
    def test_reset_invalida_status_tambem_depois_da_request(self, request):
        cache.set("wa_status:user-1", {"fase": "reconectando"}, timeout=30)

        def resposta_com_poll_concorrente(*_args, **_kwargs):
            # Simula um GET que terminou durante o reset e repopulou o cache
            # depois da primeira invalidação.
            cache.set("wa_status:user-1", {"fase": "inativo"}, timeout=30)
            return Mock(json=lambda: {"sucesso": True, "status": {"fase": "iniciando"}})

        request.side_effect = resposta_com_poll_concorrente

        resultado = whatsapp_client.reiniciar_com_qr("user-1")

        self.assertTrue(resultado["sucesso"])
        self.assertIsNone(cache.get("wa_status:user-1"))

    @patch("apps.scrapers.whatsapp_client.requests.request")
    def test_reset_uses_the_atomic_node_endpoint_without_retry(self, request):
        request.return_value = Mock(json=lambda: {
            "sucesso": True,
            "auth_removido": True,
            "status": {"fase": "iniciando"},
        })

        resultado = whatsapp_client.reiniciar_com_qr("user-42")

        self.assertTrue(resultado["sucesso"])
        request.assert_called_once_with(
            "POST", "http://whatsapp.internal:3000/api/sessoes/reset",
            headers={"x-api-key": "secret", "Content-Type": "application/json"},
            params=None, json={"session": "user-42"}, timeout=25,
        )


class WhatsAppIsolationTests(SimpleTestCase):
    @patch("apps.scrapers.whatsapp_client.status")
    def test_connection_monitor_checks_the_requested_session(self, status):
        status.return_value = {"conectado": True}
        self.assertTrue(wa_conectado("user-42"))
        status.assert_called_once_with("user-42")

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
        WHATSAPP_API_KEY="secret",
    )
    @patch("apps.scrapers.whatsapp_client.requests.request")
    def test_session_start_is_an_explicit_post_for_one_session(self, request):
        # Renomeado de "..._only_by_explicit_command": o loop de envio também
        # chama iniciar_sessao agora (ofertas._wa_pronto), então "só o usuário
        # inicia sessão" deixou de ser verdade. O que este teste sempre travou —
        # e segue travando — é a FORMA: um POST explícito, para uma sessão
        # nomeada. Quem nunca pode iniciar sessão é o GET /api/status.
        response = Mock()
        response.json.return_value = {"sucesso": True, "instancia": "user-42"}
        request.return_value = response

        result = whatsapp_client.iniciar_sessao("user-42")

        self.assertTrue(result["sucesso"])
        request.assert_called_once_with(
            "POST", "http://whatsapp.internal:3000/api/sessoes",
            headers={"x-api-key": "secret", "Content-Type": "application/json"},
            params=None, json={"session": "user-42"}, timeout=10,
        )

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
        WHATSAPP_API_KEY="secret",
    )
    @patch("apps.scrapers.whatsapp_client.requests.post")
    def test_send_routes_to_the_users_session(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {"sucesso": True, "mensagem_id": "abc123"}
        post.return_value = response

        result = whatsapp_client.enviar_oferta(
            "123@g.us", "mensagem", session="user-42"
        )

        self.assertTrue(result["sucesso"])
        self.assertEqual(post.call_args.kwargs["json"]["session"], "user-42")
        self.assertEqual(post.call_args.kwargs["json"]["grupoid"], "123@g.us")
        self.assertEqual(post.call_args.kwargs["timeout"], 65)

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
        WHATSAPP_API_KEY="secret",
    )
    @patch("apps.scrapers.whatsapp_client.requests.post")
    def test_send_rejects_success_without_message_confirmation(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {"sucesso": True}
        post.return_value = response

        result = whatsapp_client.enviar_oferta(
            "123@g.us", "mensagem", session="user-42"
        )

        self.assertFalse(result["sucesso"])
        self.assertIn("ID de confirmação", result["erro"])

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
        WHATSAPP_API_KEY="secret",
    )
    @patch("apps.scrapers.whatsapp_client.requests.post")
    def test_send_preserva_resultado_incerto_do_node(self, post):
        response = Mock(status_code=503)
        response.json.return_value = {
            "sucesso": False, "classe": "transitorio", "resultado": "incerto",
            "repetir": False, "etapa": "sendMessage", "duracao_ms": 55000,
        }
        post.return_value = response

        result = whatsapp_client.enviar_oferta("123@g.us", "mensagem", session="user-42")

        self.assertEqual(result["resultado"], "incerto")
        self.assertFalse(result["repetir"])
        self.assertEqual(result["classe"], "transitorio")


class WhatsAppDesconectarTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("wa-logout", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)
        self.url = reverse("scraper-whatsapp-desconectar")

    def test_disconnect_requires_post(self):
        # Desparear é efeito colateral: GET deixaria a rota sem proteção CSRF.
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_disconnect_requires_login(self):
        self.client.logout()
        response = self.client.post(self.url)
        self.assertIn(response.status_code, (302, 403))

    @patch("apps.scrapers.whatsapp_client.desconectar")
    def test_disconnect_targets_the_users_own_session(self, desconectar):
        desconectar.return_value = {"sucesso": True, "auth_removido": True}
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["sucesso"])
        desconectar.assert_called_once_with(self.user.perfil.sessao_whatsapp())


class WhatsAppCancelarReconexaoTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("wa-reset", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)
        self.url = reverse("scraper-whatsapp-cancelar")

    def test_reset_requires_post(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_reset_requires_login(self):
        self.client.logout()
        response = self.client.post(self.url)
        self.assertIn(response.status_code, (302, 403))

    @patch("apps.scrapers.whatsapp_client.iniciar_sessao")
    @patch("apps.scrapers.whatsapp_client.desconectar")
    @patch("apps.scrapers.whatsapp_client.reiniciar_com_qr")
    def test_reset_is_one_atomic_call_for_the_users_session(
        self, reiniciar, desconectar, iniciar
    ):
        reiniciar.return_value = {
            "sucesso": True,
            "auth_removido": True,
            "status": {"fase": "iniciando"},
        }

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["sucesso"])
        reiniciar.assert_called_once_with(self.user.perfil.sessao_whatsapp())
        desconectar.assert_not_called()
        iniciar.assert_not_called()

    @patch("apps.scrapers.whatsapp_client.reiniciar_com_qr")
    def test_reset_failure_is_returned_without_automatic_recovery(self, reiniciar):
        reiniciar.return_value = {
            "sucesso": False,
            "auth_removido": False,
            "mensagem": "Não foi possível descartar a sessão antiga.",
        }

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["sucesso"])


class WhatsAppRefreshGruposTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("wa-refresh", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)
        self.url = reverse("scraper-whatsapp-refresh")

    def test_refresh_requires_post(self):
        # Dispara getChats no Chromium: em GET a rota ficava sem proteção CSRF,
        # acionável por um <img src> de qualquer site.
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_refresh_requires_login(self):
        self.client.logout()
        response = self.client.post(self.url)
        self.assertIn(response.status_code, (302, 403))

    @patch("apps.scrapers.whatsapp_client.refresh_grupos")
    def test_refresh_targets_the_users_own_session(self, refresh):
        refresh.return_value = {"sucesso": True, "grupos": []}
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["sucesso"])
        refresh.assert_called_once_with(self.user.perfil.sessao_whatsapp())


class WhatsAppTransportContractTests(SimpleTestCase):
    """O front distingue "Node fora do ar" de "WhatsApp desconectado" pela
    presença da chave `erro`. Ela só pode aparecer por falha de transporte."""

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
        WHATSAPP_API_KEY="secret",
    )
    @patch("apps.scrapers.whatsapp_client.requests.request")
    def test_unreachable_worker_is_reported_as_erro(self, request):
        request.side_effect = OSError("connection refused")
        self.assertIn("erro", whatsapp_client.listar_grupos("user-42"))

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
        WHATSAPP_API_KEY="secret",
    )
    @patch("apps.scrapers.whatsapp_client.requests.request")
    def test_refresh_never_retries_a_non_idempotent_post(self, request):
        # O Node pode ter ACEITO o refresh e só demorado a responder: repetir
        # dispara um segundo getChats no mesmo Chromium e dobra a espera para 60s.
        request.side_effect = OSError("timed out")

        data = whatsapp_client.refresh_grupos("user-42")

        self.assertEqual(request.call_count, 1)
        self.assertIn("erro", data)
        self.assertFalse(data["sucesso"])

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
        WHATSAPP_API_KEY="secret",
    )
    @patch("apps.scrapers.whatsapp_client.requests.request")
    def test_a_healthy_worker_reply_is_passed_through_untouched(self, request):
        response = Mock()
        # Sessão viva sincronizando: NÃO pode virar "erro" para o front.
        response.json.return_value = {
            "conectado": True, "fase": "conectado", "sincronizando": True, "grupos": [],
        }
        request.return_value = response

        data = whatsapp_client.listar_grupos("user-42")

        self.assertNotIn("erro", data)
        self.assertTrue(data["conectado"])

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
        WHATSAPP_API_KEY="secret",
    )
    @patch("apps.scrapers.whatsapp_client.requests.request")
    def test_logout_does_not_retry(self, request):
        response = Mock()
        response.json.return_value = {"sucesso": True}
        request.return_value = response

        whatsapp_client.desconectar("user-42")

        request.assert_called_once_with(
            "POST", "http://whatsapp.internal:3000/api/sessoes/logout",
            headers={"x-api-key": "secret", "Content-Type": "application/json"},
            params=None, json={"session": "user-42"}, timeout=25,
        )


class WhatsAppErrorTaxonomyTests(SimpleTestCase):
    """Toda falha de envio carrega `classe`. O orquestrador decide por ela se
    conta a falha contra a regra do usuário — ver EnvioResilienciaTests."""

    def _post(self, **kwargs):
        return patch("apps.scrapers.whatsapp_client.requests.post", **kwargs)

    @override_settings(WHATSAPP_API_URL="http://wa.internal:3000", WHATSAPP_API_KEY="k")
    def test_node_classification_wins_over_the_status_code(self):
        # O Node responde 503 para toda falha de envio, inclusive as permanentes
        # (grupo apagado). Sem ler o corpo, o status sozinho diria "transitório"
        # e a regra quebrada nunca pausaria.
        response = Mock(status_code=503)
        response.json.return_value = {
            "sucesso": False,
            "erro": "Grupo de destino nao encontrado nesta conta do WhatsApp.",
            "classe": "permanente",
        }
        with self._post(return_value=response):
            r = whatsapp_client.enviar_oferta("123@g.us", "m", session="u")
        self.assertEqual(r["classe"], "permanente")

    @override_settings(WHATSAPP_API_URL="http://wa.internal:3000", WHATSAPP_API_KEY="k")
    def test_timeout_and_refused_connection_are_transient(self):
        # Os dois piores casos nunca chegam classificados pelo Node — ele não
        # chegou a responder. São exatamente os que desligavam a automação.
        for erro in (requests.Timeout("read timeout"),
                     requests.ConnectionError("connection refused")):
            with self.subTest(erro=type(erro).__name__), self._post(side_effect=erro):
                r = whatsapp_client.enviar_oferta("123@g.us", "m", session="u")
            self.assertFalse(r["sucesso"])
            self.assertEqual(r["classe"], "transitorio")

    @override_settings(WHATSAPP_API_URL="http://wa.internal:3000", WHATSAPP_API_KEY="k")
    def test_rate_limit_is_transient_and_bad_request_is_permanent(self):
        casos = [(429, "transitorio"), (500, "transitorio"), (400, "permanente")]
        for status, esperado in casos:
            response = Mock(status_code=status)
            response.json.return_value = {"erro": "x"}   # Node antigo: sem classe
            with self.subTest(status=status), self._post(return_value=response):
                r = whatsapp_client.enviar_oferta("123@g.us", "m", session="u")
            self.assertEqual(r["classe"], esperado)

    @override_settings(WHATSAPP_API_URL="http://wa.internal:3000", WHATSAPP_API_KEY="k")
    def test_node_regression_does_not_punish_the_user(self):
        # sucesso sem mensagem_id é bug nosso, não da configuração dele.
        response = Mock(status_code=200)
        response.json.return_value = {"sucesso": True}
        with self._post(return_value=response):
            r = whatsapp_client.enviar_oferta("123@g.us", "m", session="u")
        self.assertFalse(r["sucesso"])
        self.assertEqual(r["classe"], "transitorio")


class EnvioResilienciaTests(TestCase):
    """Uma indisponibilidade transitória do WhatsApp não pode desligar a
    automação nem queimar o pool de candidatos. Era o defeito relatado: o worker
    ficava fora do ar, e algumas horas depois a regra estava `ativo=False`."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("envio-user", password="test")
        self.user.perfil.marcar_verificado()
        self.cfg = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="123@g.us", canal="whatsapp",
            janela_inicio=0, janela_fim=0,       # janela 24h: o teste não depende da hora
            pausar_apos_falhas=3,
        )

    def _processar(self, status, envio=None):
        with patch("apps.scrapers.whatsapp_client.status", return_value=status) as st, \
             patch("apps.scrapers.whatsapp_client.iniciar_sessao") as iniciar, \
             patch("apps.scrapers.ofertas.selecionar_e_enviar",
                   return_value=envio or {"sucesso": True}) as enviar:
            resultados = ofertas.processar_configs_de_envio()
        self.cfg.refresh_from_db()
        return st, iniciar, enviar, resultados

    def test_disconnected_session_skips_the_pool_entirely(self):
        # O ponto caro: sem o gate, selecionar_e_enviar rodaria 8 candidatos a
        # ~30s de Playwright cada para só então descobrir que não há WhatsApp.
        _, _, enviar, _ = self._processar({"conectado": False, "fase": "reconectando"})
        enviar.assert_not_called()
        self.assertEqual(self.cfg.falhas_consecutivas, 0)
        self.assertTrue(self.cfg.ativo)

    def test_unreachable_worker_is_not_the_configs_fault(self):
        _, _, enviar, _ = self._processar({"erro": "connection refused", "conectado": False})
        enviar.assert_not_called()
        self.assertEqual(self.cfg.falhas_consecutivas, 0)
        self.assertTrue(self.cfg.ativo)

    def test_inactive_session_is_revived_but_this_tick_does_not_send(self):
        # 'inativo' é o único estado em que POST /api/sessoes reconecta sem
        # humano (credencial no volume, sessão fora do Map). initializeSession é
        # assíncrono: quem envia é o tick seguinte.
        _, iniciar, enviar, _ = self._processar({"conectado": False, "fase": "inativo"})
        iniciar.assert_called_once_with(self.user.perfil.sessao_whatsapp())
        enviar.assert_not_called()
        self.assertTrue(self.cfg.ativo)

    def test_expired_session_is_not_revived_from_the_send_loop(self):
        # O Node só chega em 'expirado' depois de purgar a credencial, então
        # revivê-lo aqui não reconecta ninguém: só fabrica um QR que ninguém está
        # olhando e prende um dos 4 slots de Chromium.
        _, iniciar, enviar, _ = self._processar({"conectado": False, "fase": "expirado"})
        iniciar.assert_not_called()
        enviar.assert_not_called()

    def test_session_state_is_read_once_per_tick_not_once_per_config(self):
        ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="456@g.us", canal="whatsapp",
            janela_inicio=0, janela_fim=0,
        )
        st, _, _, _ = self._processar({"conectado": False, "fase": "reconectando"})
        self.assertEqual(st.call_count, 1)

    def test_transient_send_failures_never_pause_the_config(self):
        # O cenário relatado, encenado: o worker pisca mais vezes que o teto.
        falha = {"sucesso": False, "motivo": "Falha de transporte: timeout",
                 "classe": "transitorio"}
        for _ in range(self.cfg.pausar_apos_falhas + 2):
            self.cfg.proximo_envio = None      # vence de novo
            self.cfg.save(update_fields=["proximo_envio"])
            self._processar({"conectado": True}, envio=falha)

        self.assertTrue(self.cfg.ativo)
        self.assertEqual(self.cfg.falhas_consecutivas, 0)

    def test_an_empty_pool_is_not_a_failure(self):
        vazio = {"sucesso": False, "motivo": "sem item elegível", "classe": "transitorio"}
        for _ in range(self.cfg.pausar_apos_falhas + 1):
            self.cfg.proximo_envio = None
            self.cfg.save(update_fields=["proximo_envio"])
            self._processar({"conectado": True}, envio=vazio)

        self.assertTrue(self.cfg.ativo, "nicho estreito não pode desligar a regra")

    def test_permanent_failures_still_pause_the_config(self):
        # A contrapartida: se o grupo sumiu, insistir só martela o WhatsApp.
        falha = {"sucesso": False, "motivo": "Grupo de destino nao encontrado.",
                 "classe": "permanente"}
        for _ in range(self.cfg.pausar_apos_falhas):
            self.cfg.proximo_envio = None
            self.cfg.save(update_fields=["proximo_envio"])
            self._processar({"conectado": True}, envio=falha)

        self.assertFalse(self.cfg.ativo)
        self.assertIn("Grupo de destino", self.cfg.motivo_pausa)

    def test_unclassified_failure_keeps_the_old_behaviour(self):
        # Node antigo no ar, ou o throw minificado do bundle: na dúvida, conta.
        falha = {"sucesso": False, "motivo": "erro estranho"}
        for _ in range(self.cfg.pausar_apos_falhas):
            self.cfg.proximo_envio = None
            self.cfg.save(update_fields=["proximo_envio"])
            self._processar({"conectado": True}, envio=falha)

        self.assertFalse(self.cfg.ativo)


class SelecionarEEnviarAbortTests(TestCase):
    def test_a_transient_failure_aborts_the_candidate_pool(self):
        # Mesma lógica que precisa_login_ml já tinha: os outros 7 candidatos
        # falhariam igual, a ~30s de Playwright cada.
        produtos = [Mock(id=i) for i in range(8)]
        falha = {"sucesso": False, "motivo": "WhatsApp não está conectado",
                 "classe": "transitorio"}
        with patch("apps.scrapers.ofertas.selecionar_item_para_grupo",
                   return_value=produtos), \
             patch("apps.scrapers.ofertas.enviar_oferta_de_produto",
                   return_value=falha) as enviar:
            r = ofertas.selecionar_e_enviar(None, "123@g.us")

        self.assertEqual(enviar.call_count, 1)
        self.assertEqual(r["classe"], "transitorio")

    def test_a_permanent_failure_still_tries_the_next_candidate(self):
        # Um produto reprovado não diz nada sobre os outros: o pool existe
        # justamente para não desistir por causa de um item ruim.
        produtos = [Mock(id=i) for i in range(3)]
        falha = {"sucesso": False, "motivo": "link sem tag de afiliado",
                 "classe": "permanente"}
        with patch("apps.scrapers.ofertas.selecionar_item_para_grupo",
                   return_value=produtos), \
             patch("apps.scrapers.ofertas.enviar_oferta_de_produto",
                   return_value=falha) as enviar:
            ofertas.selecionar_e_enviar(None, "123@g.us")

        self.assertEqual(enviar.call_count, 3)

    def test_an_empty_pool_is_reported_as_transient(self):
        with patch("apps.scrapers.ofertas.selecionar_item_para_grupo", return_value=[]):
            r = ofertas.selecionar_e_enviar(None, "123@g.us")
        self.assertEqual(r["classe"], "transitorio")


class AmazonConexaoTests(TestCase):
    """A tag de afiliado é tudo que a Amazon exige. Exigir também as credenciais da
    Creators API era um beco sem saída: elas só saem para contas com 10 vendas em
    30 dias, então quem ainda não vendeu — justamente o público da ferramenta —
    nunca conseguia "conectar", mesmo com a Amazon funcionando."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("amz", password="test")
        self.perfil = self.user.perfil

    def test_tag_alone_connects_amazon(self):
        self.perfil.afiliado_tag_amazon = "pedromachad06-20"
        self.assertTrue(self.perfil.amazon_conectado())

    def test_credentials_are_not_required_to_connect(self):
        self.perfil.afiliado_tag_amazon = "pedromachad06-20"
        self.assertEqual(self.perfil.amazon_credential_id, "")
        self.assertEqual(self.perfil.amazon_credential_secret, "")
        self.assertTrue(
            self.perfil.amazon_conectado(),
            "credenciais da Creators API exigem 10 vendas/30d: não podem gatear a conexão",
        )

    def test_no_tag_is_not_connected(self):
        # A contrapartida: sem tag não há link comissionado, então não há conexão.
        self.perfil.amazon_credential_id = "AKIA123"
        self.perfil.amazon_credential_secret = "segredo"
        self.assertFalse(self.perfil.amazon_conectado())

    def test_creators_api_status_is_orthogonal_to_being_connected(self):
        self.perfil.afiliado_tag_amazon = "pedromachad06-20"
        self.assertTrue(self.perfil.amazon_conectado())
        self.assertFalse(self.perfil.amazon_creators_ativa())

        self.perfil.amazon_credential_id = "AKIA123"
        self.perfil.amazon_credential_secret = "segredo"
        self.assertTrue(self.perfil.amazon_creators_ativa())
        self.assertTrue(self.perfil.amazon_conectado())

    def test_store_disconnected_alert_disappears_once_the_tag_is_saved(self):
        # O aviso "Loja desconectada" no card "Precisa de atenção" da home.
        self.perfil.marcar_verificado()
        self.perfil.afiliado_tag_amazon = "pedromachad06-20"
        self.perfil.save(update_fields=["afiliado_tag_amazon"])
        self.client.force_login(self.user)

        # A view importa de monitor_conexao dentro da função: o patch tem de ser na
        # origem, não em views. Sem WhatsApp de propósito — o alerta da loja não
        # pode depender do canal de envio.
        with patch("apps.scrapers.monitor_conexao.wa_conectado", return_value=False):
            response = self.client.get(reverse("home"))

        titulos = [titulo for titulo, _texto, _rota in response.context["alertas"]]
        self.assertNotIn("Loja desconectada", titulos)

    def test_the_affiliate_link_carries_the_users_tag_without_any_credential(self):
        # O que de fato importa: a comissão sai no nome do usuário.
        from apps.scrapers.marketplaces.amazon import Amazon

        self.perfil.afiliado_tag_amazon = "pedromachad06-20"
        self.perfil.save(update_fields=["afiliado_tag_amazon"])
        produto = Produto.objects.create(
            marketplace="amazon", nome="Fone Bluetooth", asin="B0C1234XYZ",
            categoria="Áudio", preco_sem_desconto=199.0, preco_com_cupom=149.0,
        )

        mp = Amazon()
        r = mp.build_affiliate_link(produto, usuario=self.user)

        self.assertIn("tag=pedromachad06-20", r["link_afiliado"])
        self.assertTrue(r["afiliado_ok"])
        self.assertTrue(mp.verify_affiliate_tag(r["link_afiliado"], usuario=self.user))


class ReligarConfigsCommandTests(TestCase):
    """One-shot de reparo: corrigir o código não desfaz o que já está no banco."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("religar-user", password="test")

    def _cfg(self, motivo, grupo="1@g.us"):
        return ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id=grupo, ativo=False,
            falhas_consecutivas=5, motivo_pausa=motivo,
        )

    def _rodar(self, *args):
        saida = StringIO()
        call_command("religar_configs", *args, stdout=saida)
        return saida.getvalue()

    def test_transient_pause_is_undone_and_the_counter_is_cleared(self):
        cfg = self._cfg("Falha de transporte: read timeout")
        self._rodar()
        cfg.refresh_from_db()
        self.assertTrue(cfg.ativo)
        self.assertEqual(cfg.falhas_consecutivas, 0)
        self.assertEqual(cfg.motivo_pausa, "")

    def test_a_genuinely_broken_config_stays_paused(self):
        # Religar esta só produziria falha nova: o grupo não existe mais.
        cfg = self._cfg("Grupo de destino nao encontrado nesta conta do WhatsApp.")
        self._rodar()
        cfg.refresh_from_db()
        self.assertFalse(cfg.ativo)

    def test_dry_run_writes_nothing(self):
        cfg = self._cfg("sem item elegível")
        saida = self._rodar("--dry-run")
        cfg.refresh_from_db()
        self.assertFalse(cfg.ativo)
        self.assertIn("dry-run", saida)


class ConfiguracaoValidationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("config-user", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)
        self.url = reverse("scraper-configuracoes")

    def test_rejects_malformed_numeric_values_without_server_error(self):
        response = self.client.post(self.url, {
            "canal": "whatsapp",
            "grupo_id": "123@g.us",
            "intervalo_minutos": "nao-e-numero",
        })

        self.assertRedirects(response, self.url)
        self.assertFalse(self.user.configuracoes.exists())
        self.assertTrue(any(
            "valor inválido" in str(message)
            for message in get_messages(response.wsgi_request)
        ))

    def test_rejects_invalid_schedule_range(self):
        response = self.client.post(self.url, {
            "canal": "whatsapp",
            "grupo_id": "123@g.us",
            "intervalo_minutos": "60",
            "janela_inicio": "24",
            "janela_fim": "8",
            "min_desconto_percent": "15",
        })

        self.assertRedirects(response, self.url)
        self.assertFalse(self.user.configuracoes.exists())


class TopPromocoesFilterTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("deals-user", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)
        self.url = reverse("scraper-top")
        self.fone = self._criar_produto(
            marketplace="mercadolivre", nome="Fone Bluetooth", categoria="Áudio",
            macro_categoria="Eletrônicos", preco_sem_desconto=100, preco_com_cupom=50,
            link_produto="https://example.com/fone",
        )
        self.cafeteira = self._criar_produto(
            marketplace="amazon", owner=self.user, nome="Cafeteira",
            categoria="Cozinha", macro_categoria="Casa", preco_sem_desconto=100,
            preco_com_cupom=90, link_produto="https://example.com/cafeteira",
        )

    def _criar_produto(self, afiliado=True, **campos):
        """Produto de fixture já afiliado — a listagem só mostra item com link.

        Testar filtro (busca, loja, cupom vencido) com item não afiliado dava lista
        vazia por um motivo que não era o do teste.
        """
        campos.setdefault("origem", "oferta")
        produto = Produto.objects.create(**campos)
        if afiliado:
            self._afiliar(produto)
        return produto

    def _afiliar(self, produto):
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto,
            link_afiliado=f"https://meli.la/{produto.id}",
            url_isca=produto.link_produto, afiliado_ok=True, estado="pronto",
        )
        return produto

    def test_search_and_minimum_discount_are_applied(self):
        response = self.client.get(self.url, {"q": "fone", "min_desconto": "40"})

        self.assertEqual([p.nome for p in response.context["produtos"]], ["Fone Bluetooth"])

    def test_filters_are_restored_on_next_visit_and_can_be_cleared(self):
        self.client.get(self.url, {"loja": "amazon", "ordenar": "valor"})

        response = self.client.get(self.url)
        self.assertEqual(response.context["loja_selecionada"], "amazon")
        self.assertEqual([p.nome for p in response.context["produtos"]], ["Cafeteira"])

        self.client.get(self.url, {"reset": "1"})
        response = self.client.get(self.url)
        self.assertEqual(response.context["loja_selecionada"], "")
        self.assertEqual(len(response.context["produtos"]), 2)

    def test_expired_coupon_is_not_attached_to_top_promotion(self):
        product = self._criar_produto(
            marketplace="mercadolivre",
            nome="Panela com cupom vencido",
            categoria="Cozinha",
            macro_categoria="Casa",
            campanha_id="expired-coupon",
            preco_sem_desconto=200,
            preco_com_cupom=120,
            link_produto="https://example.com/panela",
        )
        Cupom.objects.create(
            campanha_id="expired-coupon", titulo="Cupom vencido",
            tipo_desconto="fixo", valor_desconto=80, valor_minimo=0,
            link_original="https://example.com/coupon", estado="ativo",
            validade=timezone.now() - timedelta(days=1),
        )

        response = self.client.get(self.url, {"q": "Panela com cupom vencido"})

        [rendered] = [p for p in response.context["produtos"] if p.id == product.id]
        self.assertIsNone(rendered.cupom)

    def test_stale_products_are_hidden_from_top_promotions(self):
        # Afiliado de propósito: assim o teste prova que é o `estado` que esconde o
        # item, e não o filtro de afiliação.
        stale = self._criar_produto(
            marketplace="mercadolivre",
            nome="Oferta velha",
            categoria="Cozinha",
            macro_categoria="Casa",
            preco_sem_desconto=100,
            preco_com_cupom=50,
            link_produto="https://example.com/stale",
            estado="stale",
        )

        response = self.client.get(self.url, {"q": "Oferta velha"})

        self.assertNotIn(stale.id, [p.id for p in response.context["produtos"]])

    def test_products_without_affiliate_link_are_hidden_from_sending_list(self):
        """Item sem link de afiliado não pode chegar ao botão Enviar: enviá-lo não
        comissiona nada. Antes ele aparecia com o badge 'pendente' e era enviável."""
        pendente = self._criar_produto(
            afiliado=False,
            marketplace="mercadolivre",
            nome="Fritadeira sem link",
            categoria="Cozinha",
            macro_categoria="Casa",
            preco_sem_desconto=200,
            preco_com_cupom=100,
            link_produto="https://example.com/fritadeira",
        )

        response = self.client.get(self.url, {"q": "Fritadeira"})

        self.assertNotIn(pendente.id, [p.id for p in response.context["produtos"]])
        self.assertEqual(response.context["pendentes_ocultos"], 1)
        self.assertTrue(response.context["so_afiliados"])

    def test_pending_products_are_visible_under_the_diagnostic_filter(self):
        pendente = self._criar_produto(
            afiliado=False,
            marketplace="mercadolivre",
            nome="Fritadeira sem link",
            categoria="Cozinha",
            macro_categoria="Casa",
            preco_sem_desconto=200,
            preco_com_cupom=100,
            link_produto="https://example.com/fritadeira",
        )

        response = self.client.get(self.url, {"q": "Fritadeira", "afiliado": "todos"})

        self.assertIn(pendente.id, [p.id for p in response.context["produtos"]])
        self.assertFalse(response.context["so_afiliados"])

    def test_generating_affiliate_links_requires_login_but_not_staff(self):
        """A fila é por usuário e a lista só mostra item afiliado: sem esta rota, quem
        não é staff dependia só do worker para ter QUALQUER produto enviável."""
        url = reverse("scraper-gerar-links")
        self.assertFalse(self.user.is_staff)

        self.client.logout()
        anonima = self.client.get(url)
        self.assertEqual(anonima.status_code, 302)
        self.assertIn("/login", anonima["Location"])

    def test_legacy_product_level_affiliate_link_still_counts_as_ready(self):
        """Item afiliado antes do multi-tenant tem o link no próprio Produto e nenhuma
        linha em LinkAfiliadoUsuario. Não pode sumir da tela por causa disso."""
        legado = self._criar_produto(
            afiliado=False,
            marketplace="mercadolivre",
            nome="Item legado",
            categoria="Cozinha",
            macro_categoria="Casa",
            preco_sem_desconto=200,
            preco_com_cupom=100,
            link_produto="https://example.com/legado",
            link_afiliado="https://meli.la/legado",
        )

        response = self.client.get(self.url, {"q": "Item legado"})

        self.assertIn(legado.id, [p.id for p in response.context["produtos"]])

    def test_source_health_hides_disabled_and_inapplicable_connectors(self):
        FonteIngestao.objects.filter(slug="mercadolivre-web").update(status="ok")
        FonteIngestao.objects.filter(slug="amazon-public-web").update(status="degraded")
        FonteIngestao.objects.filter(slug="promobit-community").update(
            habilitada=False, status="disabled")

        response = self.client.get(self.url)

        self.assertEqual([source.slug for source in response.context["fontes"]],
                         ["mercadolivre-web"])

    @patch("apps.scrapers.scraper_mercadolivre.ofertas_scraper.mapear_ofertas",
           return_value=12)
    def test_flash_scrape_marks_mercado_livre_source_healthy(self, _mapear):
        source = FonteIngestao.objects.get(slug="mercadolivre-web")
        source.status = "degraded"
        source.falhas_consecutivas = 2
        source.erro_publico = "timeout"
        source.save()
        from apps.scrapers.management.commands.automacao import _rodar_scrape_rapido

        self.assertEqual(_rodar_scrape_rapido(paginas=2), 12)
        source.refresh_from_db()
        self.assertEqual(source.status, "ok")
        self.assertEqual(source.falhas_consecutivas, 0)
        self.assertEqual(source.erro_publico, "")


class AttributionWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("operator", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)
        self.product = Produto.objects.create(
            marketplace="mercadolivre", nome="Oferta rastreável", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=60,
            link_produto="https://example.com/product",
        )

    def test_signed_redirect_records_anonymous_click(self):
        from django.core import signing
        publication = Publicacao.objects.create(
            usuario=self.user, produto=self.product, canal="whatsapp",
            destino_id="group@g.us", status="enviado",
            link_afiliado="https://example.com/affiliate",
        )
        token = signing.dumps({"p": str(publication.id_publico)}, salt="click")

        response = self.client.get(reverse("scraper-redirect", args=[token]))

        self.assertRedirects(
            response, "https://example.com/affiliate", fetch_redirect_response=False)
        self.assertEqual(CliquePublicacao.objects.filter(publicacao=publication).count(), 1)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_invalid_redirect_token_is_not_open_redirect(self):
        response = self.client.get(reverse("scraper-redirect", args=["not-a-real-token"]))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(CliquePublicacao.objects.exists())

    def test_short_redirect_records_anonymous_click(self):
        publication = Publicacao.objects.create(
            usuario=self.user, produto=self.product, canal="whatsapp",
            destino_id="group@g.us", status="enviado",
            link_afiliado="https://example.com/affiliate",
        )
        self.assertTrue(publication.slug_curto)

        response = self.client.get(
            reverse("redirect-curto", args=[publication.slug_curto]))

        self.assertRedirects(
            response, "https://example.com/affiliate", fetch_redirect_response=False)
        self.assertEqual(CliquePublicacao.objects.filter(publicacao=publication).count(), 1)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_short_redirect_rejects_unknown_slug_and_pending_publication(self):
        pending = Publicacao.objects.create(
            usuario=self.user, produto=self.product, canal="whatsapp",
            destino_id="group@g.us", status="pendente",
            link_afiliado="https://example.com/affiliate",
        )
        for slug in ["nao-existe", pending.slug_curto]:
            response = self.client.get(reverse("redirect-curto", args=[slug]))
            self.assertEqual(response.status_code, 404)
        self.assertFalse(CliquePublicacao.objects.exists())

    def test_operational_log_sanitizes_sensitive_context(self):
        from apps.scrapers.eventos import log_event

        log_event("sistema", "secret_test", "testing", usuario=self.user,
                  contexto={"api_key": "super-secret", "safe": "ok"})

        event = EventoOperacional.objects.get(evento="secret_test")
        self.assertEqual(event.contexto["api_key"], "***")
        self.assertEqual(event.contexto["safe"], "ok")

    @patch("apps.scrapers.views.wa_conectado", create=True)
    def test_dashboard_is_the_authenticated_home(self, _wa):
        with (
            patch("apps.scrapers.monitor_conexao.wa_conectado", return_value=False),
            patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=False),
        ):
            response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sua operação")

    def test_home_mostra_copy_amigavel_nunca_erro_tecnico_cru(self):
        # Regressão: str(exc) cru de RelatorioSync.erro e Publicacao.erro vazava na
        # home, e um {#..#} multi-linha renderizava como texto no card de receita.
        RelatorioSync.objects.create(
            usuario=self.user, marketplace="mercadolivre", status="erro",
            erro="Traceback: ML_AFFILIATE_REPORT_URL sem tabela detectável")
        RelatorioSync.objects.create(
            usuario=self.user, marketplace="amazon", status="nao_configurado")
        Publicacao.objects.create(
            usuario=self.user, produto=self.product, canal="whatsapp",
            destino_id="group@g.us", status="falhou",
            erro="Timeout de 45s no getState do WhatsApp")

        with (
            patch("apps.scrapers.monitor_conexao.wa_conectado", return_value=False),
            patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=False),
        ):
            response = self.client.get(reverse("home"))

        self.assertNotContains(response, "Traceback")
        self.assertNotContains(response, "ML_AFFILIATE_REPORT_URL")
        self.assertNotContains(response, "getState")
        self.assertNotContains(response, "Sem botão")
        self.assertContains(response, "Falha temporária na leitura dos relatórios")
        self.assertContains(response, "O WhatsApp demorou para responder ao envio.")
        self.assertContains(
            response, "Esta loja ainda não tem leitura automática de relatórios.")

    @patch("apps.scrapers.relatorios.ADAPTERS")
    def test_automatic_report_sync_is_idempotent(self, adapters):
        from datetime import date
        from apps.scrapers.relatorios import ReportRow, sync_marketplace

        adapter = Mock()
        adapter.fetch.return_value = [ReportRow(
            marketplace="mercadolivre", data=date(2026, 7, 9),
            etiqueta="grupo-casa", produto_nome="Fone", cliques=10,
            pedidos=2, receita=199.90, comissao=20.00,
        )]
        adapters.__contains__.side_effect = lambda key: key == "mercadolivre"
        adapters.__getitem__.side_effect = lambda key: adapter

        sync_marketplace(self.user, "mercadolivre")
        sync_marketplace(self.user, "mercadolivre")

        self.assertEqual(ReceitaAfiliado.objects.filter(usuario=self.user).count(), 1)
        receita = ReceitaAfiliado.objects.get(usuario=self.user)
        self.assertEqual(receita.cliques, 10)
        self.assertEqual(receita.origem, "auto")
        self.assertTrue(EventoOperacional.objects.filter(
            pipeline="relatorios", evento="sync_ok", usuario=self.user).exists())

    @patch("apps.scrapers.relatorios.sync_marketplace")
    def test_botao_sincronizar_agenda_e_nao_executa_no_request(self, sync_marketplace):
        # O sync sobe um Chromium (Playwright, goto de 45s). Rodar isso dentro do
        # request punha um browser inteiro no processo do gunicorn, contra o timeout
        # de 120s. Agora a view só marca o registro como vencido e o worker executa.
        antes = timezone.now()

        response = self.client.post(reverse("scraper-sincronizar-receitas"), {
            "marketplace": "mercadolivre",
        })

        self.assertRedirects(response, reverse("home"))
        sync_marketplace.assert_not_called()
        sync = RelatorioSync.objects.get(usuario=self.user, marketplace="mercadolivre")
        self.assertIsNotNone(sync.proxima_execucao)
        self.assertGreaterEqual(sync.proxima_execucao, antes)
        self.assertLessEqual(sync.proxima_execucao, timezone.now())

    def test_botao_sincronizar_recusa_marketplace_invalido(self):
        response = self.client.post(reverse("scraper-sincronizar-receitas"), {
            "marketplace": "shopee",
        })

        self.assertRedirects(response, reverse("home"))
        self.assertFalse(RelatorioSync.objects.filter(marketplace="shopee").exists())

    @patch("apps.scrapers.ofertas._baixar_imagem_b64", return_value=(None, None))
    @patch("apps.scrapers.senders.whatsapp.WhatsAppSender.enviar_oferta")
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.verify_link")
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.build_affiliate_link")
    def test_failed_publication_writes_operational_event(
        self, build_link, verify_link, send, _img
    ):
        from apps.scrapers.ofertas import enviar_oferta_de_produto
        build_link.return_value = {
            "link_afiliado": "https://example.com/a?tracking_id=ok",
            "afiliado_ok": True,
        }
        verify_link.return_value = {"ok": True}
        send.return_value = {"sucesso": False, "erro": "WhatsApp desconectado"}

        result = enviar_oferta_de_produto(
            self.product, "group@g.us", usuario=self.user, destino_nome="Grupo")

        self.assertFalse(result["sucesso"])
        self.assertTrue(EventoOperacional.objects.filter(
            pipeline="publicacao", evento="send_failed", usuario=self.user).exists())

    @patch("apps.scrapers.ofertas._baixar_imagem_b64", return_value=(None, None))
    @patch("apps.scrapers.senders.whatsapp.WhatsAppSender.enviar_oferta")
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.verify_link")
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.build_affiliate_link")
    def test_uncertain_whatsapp_delivery_is_recorded_without_retry(
        self, build_link, verify_link, send, _img
    ):
        from apps.scrapers.ofertas import enviar_oferta_de_produto
        build_link.return_value = {
            "link_afiliado": "https://example.com/a?tracking_id=ok",
            "afiliado_ok": True,
        }
        verify_link.return_value = {"ok": True}
        send.return_value = {
            "sucesso": False, "erro": "confirmação pendente", "classe": "transitorio",
            "resultado": "incerto", "repetir": False, "etapa": "sendMessage",
            "duracao_ms": 55000,
        }

        result = enviar_oferta_de_produto(
            self.product, "group@g.us", usuario=self.user, destino_nome="Grupo")

        self.assertEqual(result["resultado"], "incerto")
        self.assertFalse(result["repetir"])
        self.assertEqual(Publicacao.objects.get(usuario=self.user).status, "incerto")
        self.assertTrue(EventoOperacional.objects.filter(
            pipeline="whatsapp", evento="send_timeout", usuario=self.user).exists())

    @patch("apps.scrapers.ofertas._baixar_imagem_b64", return_value=(None, None))
    @patch("apps.scrapers.senders.registry.get_sender")
    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_successful_delivery_records_history_without_legacy_key(
        self, get_marketplace, get_sender, _image
    ):
        from apps.scrapers.ofertas import enviar_oferta_de_produto
        from apps.scrapers.senders.base import WhatsAppMarkup

        marketplace = Mock()
        marketplace.build_affiliate_link.return_value = {
            "link_afiliado": "https://example.com/a?tracking_id=ok",
            "afiliado_ok": True,
        }
        get_marketplace.return_value = marketplace
        sender = Mock(markup=WhatsAppMarkup(), prefers_image="b64")
        sender.enviar_oferta.return_value = {"sucesso": True, "via": "test"}
        get_sender.return_value = sender

        result = enviar_oferta_de_produto(
            self.product, "group@g.us", verificar=False,
            usuario=self.user, destino_nome="Grupo",
        )

        self.assertTrue(result["sucesso"])
        self.assertTrue(HistoricoEnvio.objects.filter(
            produto=self.product, usuario=self.user,
        ).exists())
        self.assertEqual(
            Publicacao.objects.get(produto=self.product).status, "enviado"
        )

    def test_group_specific_branding_overrides_account_default(self):
        # A mensagem padrão agora é mínima (estilo dos grupos, sem header de marca).
        # A marca do grupo entra pelo template_a — é esse override que precede a conta.
        from apps.scrapers.ofertas import montar_mensagem
        config = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="group@g.us", nome_marca="Tech do Dia",
            chamada_acao="Ver a oferta",
            template_a="{marca}\n{nome}\nPor {preco}\n{link}",
        )
        message = montar_mensagem(
            self.product, "https://example.com/a", None,
            usuario=self.user, configuracao=config,
        )
        self.assertIn("Tech do Dia", message)

    def test_default_affiliate_disclosure_is_not_added_to_messages(self):
        from apps.scrapers.ofertas import montar_mensagem

        message = montar_mensagem(
            self.product, "https://example.com/a", None, usuario=self.user,
        )

        self.assertNotIn("Este conteúdo contém link de afiliado.", message)

    @override_settings(DEBUG=False, PUBLIC_BASE_URL="https://spreading.example")
    def test_mensagem_leva_o_link_direto_da_loja_mesmo_em_producao(self):
        """Decisão de produto: URL do sistema (…/r/<slug>/) na mensagem denuncia
        promoção automatizada. O link publicado é sempre o afiliado direto."""
        from apps.scrapers.ofertas import _link_publicado

        publication = Mock(id_publico=uuid.uuid4(), slug_curto="Ab3xK9z")
        affiliate = "https://meli.la/link-afiliado"
        self.assertEqual(_link_publicado(publication, affiliate), affiliate)
        self.assertEqual(_link_publicado(None, affiliate), affiliate)


class RankingAndCooldownTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("ranker", password="test")
        self.group_a = "casa@g.us"
        self.group_b = "tech@g.us"

    def _product(self, nome, preco_final, macro="Casa"):
        return Produto.objects.create(
            marketplace="mercadolivre", nome=nome, origem="oferta",
            macro_categoria=macro, categoria=macro,
            preco_sem_desconto=100, preco_com_cupom=preco_final,
            link_produto=f"https://example.com/{nome.replace(' ', '-')}",
        )

    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_cooldown_is_per_destination_and_allows_other_groups(self, get_marketplace):
        get_marketplace.return_value = Mock(is_alive=Mock(return_value=True))
        product = self._product("Air fryer", 70)
        Publicacao.objects.create(
            usuario=self.user, produto=product, canal="whatsapp",
            destino_id=self.group_a, status="enviado", enviada_em=timezone.now(),
            preco_final=70,
        )

        from apps.scrapers.ofertas import selecionar_item_para_grupo
        same_group = selecionar_item_para_grupo(
            usuario=self.user, grupo_id=self.group_a, min_desconto_percent=10)
        other_group = selecionar_item_para_grupo(
            usuario=self.user, grupo_id=self.group_b, min_desconto_percent=10)

        self.assertEqual(same_group, [])
        self.assertEqual(other_group, [product])

    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_cooldown_allows_evergreen_product_after_meaningful_price_drop(self, get_marketplace):
        get_marketplace.return_value = Mock(is_alive=Mock(return_value=True))
        product = self._product("Cafeteira", 70)
        Publicacao.objects.create(
            usuario=self.user, produto=product, canal="whatsapp",
            destino_id=self.group_a, status="enviado", enviada_em=timezone.now(),
            preco_final=80,
        )

        from apps.scrapers.ofertas import selecionar_item_para_grupo
        selected = selecionar_item_para_grupo(
            usuario=self.user, grupo_id=self.group_a, min_desconto_percent=10)

        self.assertEqual(selected, [product])

    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_ranking_explains_real_30_day_low(self, get_marketplace):
        get_marketplace.return_value = Mock(is_alive=Mock(return_value=True))
        product = self._product("Monitor", 70, macro="Eletrônicos")
        for price in [100, 95, 70]:
            registrar_preco("mercadolivre", "", product.link_produto, price)

        from apps.scrapers.ofertas import selecionar_item_para_grupo
        selected = selecionar_item_para_grupo(
            usuario=self.user, grupo_id=self.group_a, min_desconto_percent=10)

        self.assertEqual(selected, [product])
        self.assertIn("mínima de 30 dias", selected[0].motivos_score)

    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_active_coupon_minimum_spend_blocks_ineligible_offer(self, get_marketplace):
        get_marketplace.return_value = Mock(is_alive=Mock(return_value=True))
        product = Produto.objects.create(
            marketplace="mercadolivre", nome="Panela", origem="oferta",
            campanha_id="coupon-1", macro_categoria="Casa", categoria="Casa",
            preco_sem_desconto=100, preco_com_cupom=70,
            link_produto="https://example.com/panela",
        )
        Cupom.objects.create(
            campanha_id="coupon-1", titulo="Cupom acima do mínimo",
            tipo_desconto="fixo", valor_desconto=30, valor_minimo=150,
            link_original="https://example.com/coupon", estado="ativo",
        )

        from apps.scrapers.ofertas import selecionar_item_para_grupo
        selected = selecionar_item_para_grupo(
            usuario=self.user, grupo_id=self.group_a, min_desconto_percent=10)

        self.assertNotIn(product, selected)


class AmazonPipelineTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("amazon-user", password="test")
        self.user.perfil.afiliado_tag_amazon = "tagusuario-20"
        self.user.perfil.save(update_fields=["afiliado_tag_amazon"])

    def test_amazon_affiliate_link_uses_user_tag_and_private_cache(self):
        product = Produto.objects.create(
            marketplace="amazon", owner=self.user, asin="B012345678",
            nome="Echo", origem="oferta", preco_sem_desconto=300,
            preco_com_cupom=250,
            link_produto="https://www.amazon.com.br/dp/B012345678?ref=x",
        )

        result = amazon_link.gerar_link_afiliado_para_produto(product, usuario=self.user)

        self.assertEqual(
            result["link_afiliado"],
            "https://www.amazon.com.br/dp/B012345678?tag=tagusuario-20",
        )
        self.assertTrue(amazon_link.link_tem_tag_afiliado(result["link_afiliado"], self.user))
        self.assertTrue(LinkAfiliadoUsuario.objects.filter(
            usuario=self.user, produto=product, afiliado_ok=True).exists())

    def test_amazon_item_mapping_requires_permitted_api_price_fields(self):
        mapped = amazon_ofertas._mapear_item({
            "asin": "B000API123",
            "itemInfo": {"title": {"displayValue": "Produto API"}},
            "offersV2": {"listings": [{
                "price": {
                    "money": {"amount": 80},
                    "savingBasis": {"money": {"amount": 100}},
                },
                "merchantInfo": {"name": "Amazon.com.br"},
                "dealDetails": {"displayName": "Oferta relâmpago"},
            }]},
            "images": {"primary": {"large": {"url": "https://example.com/i.jpg"}}},
        })

        self.assertEqual(mapped["asin"], "B000API123")
        self.assertEqual(mapped["preco_sem_desconto"], 100)
        self.assertEqual(mapped["preco_com_cupom"], 80)
        self.assertTrue(mapped["tem_promocao"])

    @patch("apps.scrapers.scraper_amazon.ofertas_scraper.creators_api.search_items")
    def test_amazon_upsert_keeps_products_private_to_user(self, search_items):
        search_items.side_effect = [[{
            "asin": "BPRIVATE123",
            "itemInfo": {"title": {"displayValue": "Produto privado"}},
            "offersV2": {"listings": [{
                "price": {
                    "money": {"amount": 50},
                    "savingBasis": {"money": {"amount": 100}},
                },
            }]},
        }], []]

        with override_settings(AMAZON_FEED_KEYWORDS=["fone"], AMAZON_MIN_SAVINGS_PCT=10):
            total = amazon_ofertas.mapear_ofertas(usuario=self.user)

        self.assertEqual(total, 1)
        self.assertTrue(Produto.objects.filter(
            marketplace="amazon", asin="BPRIVATE123", owner=self.user,
            fonte="amazon-creators-api", estado="ativo",
        ).exists())


class TenantSecurityTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner", password="test")
        self.other = get_user_model().objects.create_user("other", password="test")
        self.owner.perfil.marcar_verificado()
        self.other.perfil.marcar_verificado()

    def test_user_cannot_update_another_users_destination_rule(self):
        cfg = ConfiguracaoEnvio.objects.create(
            owner=self.owner, grupo_id="owner@g.us", grupo_nome="Original",
            intervalo_minutos=60, janela_inicio=8, janela_fim=20,
            min_desconto_percent=15,
        )
        self.client.force_login(self.other)

        self.client.post(reverse("scraper-configuracoes"), {
            "id": str(cfg.id),
            "canal": "whatsapp",
            "grupo_id": "hijack@g.us",
            "grupo_nome": "Hijacked",
            "intervalo_minutos": "15",
            "janela_inicio": "8",
            "janela_fim": "20",
            "min_desconto_percent": "1",
            "max_envios_dia": "99",
            "pausar_apos_falhas": "9",
        })

        cfg.refresh_from_db()
        self.assertEqual(cfg.owner, self.owner)
        self.assertEqual(cfg.grupo_id, "owner@g.us")
        self.assertEqual(cfg.grupo_nome, "Original")


class MercadoLivreCleanupIsolationTests(TestCase):
    def test_coupon_sync_preserves_private_products_from_other_marketplaces(self):
        owner = get_user_model().objects.create_user("amazon-owner", password="test")
        private_product = Produto.objects.create(
            marketplace="amazon",
            owner=owner,
            asin="B000TEST",
            campanha_id="same-campaign",
            origem="cupom",
            nome="Produto privado",
            preco_sem_desconto=100,
            preco_com_cupom=90,
            link_produto="https://www.amazon.com.br/dp/B000TEST",
        )

        _sincronizar_produtos_no_banco([{
            "campaignId": "same-campaign",
            "produtos_aplicaveis": [],
        }])

        self.assertTrue(Produto.objects.filter(pk=private_product.pk).exists())

    def test_coupon_sync_marks_old_shared_coupon_products_stale_instead_of_deleting(self):
        old_product = Produto.objects.create(
            marketplace="mercadolivre",
            campanha_id="coupon-stale",
            origem="cupom",
            nome="Produto antigo",
            preco_sem_desconto=100,
            preco_com_cupom=80,
            link_produto="https://example.com/old",
        )

        _sincronizar_produtos_no_banco([{
            "campaignId": "coupon-stale",
            "produtos_aplicaveis": [],
        }])

        old_product.refresh_from_db()
        self.assertEqual(old_product.estado, "stale")
        self.assertIn("sincronização", old_product.falha_verificacao)


class RaspagemDeCuponsTests(TestCase):
    """Cupons pararam de vir e nada avisou.

    mapear_cupons() — o único código que popula a tabela Cupom — ficou fora do
    scrape_all: só rodava no clique manual de staff da tela de Scraper. Em produção
    a tabela ficava vazia, e link.py aborta a geração de link quando o produto tem
    campanha_id sem Cupom no banco: cupom faltando também virava link pendente.
    """

    def _patches(self, ofertas=10, cupons_codigo=3, cupons_campanha=5, campanha_erro=None):
        return (
            patch("apps.scrapers.scraper_mercadolivre.ofertas_scraper.mapear_ofertas",
                  return_value=ofertas),
            patch("apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper.mapear_cupons_codigo",
                  return_value=cupons_codigo),
            patch("apps.scrapers.scraper_mercadolivre.scraper.mapear_cupons",
                  side_effect=campanha_erro) if campanha_erro else
            patch("apps.scrapers.scraper_mercadolivre.scraper.mapear_cupons",
                  return_value=cupons_campanha),
        )

    def test_scrape_all_raspa_os_cupons_de_campanha(self):
        from apps.scrapers.models import ExecucaoIngestao

        p1, p2, p3 = self._patches()
        with p1, p2, p3 as campanha:
            get_marketplace("mercadolivre").scrape_all()

        campanha.assert_called_once()
        run = ExecucaoIngestao.objects.latest("id")
        self.assertEqual(run.total_cupons, 8)      # 3 de código + 5 de campanha
        self.assertEqual(run.status, "ok")

    def test_falha_nos_cupons_de_campanha_nao_derruba_ofertas(self):
        """O parser de campanha depende de um JSON embutido no bundle do ML — a peça
        mais frágil daqui. Se ele cair, ofertas e códigos ainda têm de entrar."""
        from apps.scrapers.models import ExecucaoIngestao

        p1, p2, p3 = self._patches(campanha_erro=RuntimeError("NORDIC sumiu"))
        with p1, p2, p3:
            get_marketplace("mercadolivre").scrape_all()

        run = ExecucaoIngestao.objects.latest("id")
        self.assertEqual(run.status, "ok")
        self.assertEqual(run.total_ofertas, 10)
        self.assertEqual(run.total_cupons, 3)      # só os de código
        self.assertTrue(EventoOperacional.objects.filter(
            evento="cupons_campanha_erro", level="warning").exists())

    def test_ofertas_sem_nenhum_cupom_vira_alerta(self):
        """800 ofertas e zero cupons era reportado como sucesso: o único sinal era o
        total zerado, e as ofertas sozinhas o mantinham positivo."""
        p1, p2, p3 = self._patches(ofertas=800, cupons_codigo=0, cupons_campanha=0)
        with p1, p2, p3:
            get_marketplace("mercadolivre").scrape_all()

        evento = EventoOperacional.objects.get(evento="cupons_vazios")
        self.assertEqual(evento.level, "warning")
        self.assertEqual(evento.contexto["ofertas"], 800)

    def test_coleta_normal_nao_alerta(self):
        p1, p2, p3 = self._patches()
        with p1, p2, p3:
            get_marketplace("mercadolivre").scrape_all()

        self.assertFalse(EventoOperacional.objects.filter(evento="cupons_vazios").exists())

    def test_evento_de_cupom_vazio_e_traduzido_na_saude(self):
        from apps.scrapers.saude import descrever

        info = descrever("cupons_vazios")

        self.assertNotEqual(info["titulo"], "cupons_vazios")   # não caiu no fallback
        self.assertTrue(info["acao"])


class ParserDeCupomDeCampanhaTests(TestCase):
    """O parser de /cupons/filter contra o DOM REAL do ML.

    Ele lê um JSON embutido num bundle do ML (#__NORDIC_RENDERING_CTX__) e o extrai
    por split de string (`_n.ctx.r=`). É a peça mais frágil da raspagem: qualquer
    rename no bundle zera os cupons. python/debug_cupom.json é um dump verdadeiro
    dessa página — o mesmo que serviu para escrever o parser.
    """

    DUMP = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))), "debug_cupom.json")

    def setUp(self):
        # A página vazia custa 3 × RETRY_WAIT de sono real. Útil contra o ML,
        # inútil aqui: sem isto a classe sozinha leva 30s.
        sono = patch("apps.scrapers.scraper_mercadolivre.scraper.time.sleep")
        sono.start()
        self.addCleanup(sono.stop)

    def _envelope(self, d):
        """Serializa um payload no formato Nordic que o parser espera, ou devolve a
        string crua (para simular uma página bloqueada / sem payload)."""
        if isinstance(d, str):
            return d
        return "_n.ctx.r=" + json.dumps(d) + ";_n.ctx.r.assets={}"

    def _conteudo_por_pagina(self, paginas):
        """paginas[i] serve a página i+1; a última se repete (o fim do laço relê a
        página vazia por causa das retries)."""
        textos = [self._envelope(p) for p in paginas]

        def conteudo(pag):
            idx = pag - 1
            return textos[idx] if idx < len(textos) else textos[-1]

        return conteudo

    def _http_falsa(self, paginas):
        """Mock de requests.Session servindo o HTML pelo número de página da URL."""
        conteudo = self._conteudo_por_pagina(paginas)

        def _get(url, **kw):
            pag = int(re.search(r"page=(\d+)", url).group(1))
            resp = Mock()
            resp.text = conteudo(pag)
            resp.raise_for_status = Mock()
            return resp

        sess = Mock()
        sess.get.side_effect = _get
        return sess

    def _browser_page(self, paginas):
        """Mock de um `page` do Playwright: goto guarda a página, content() serve o
        HTML dela — é o que o transporte usa no fallback."""
        conteudo = self._conteudo_por_pagina(paginas)
        estado = {"pag": 1}
        page = Mock()

        def _goto(url, *a, **kw):
            estado["pag"] = int(re.search(r"page=(\d+)", url).group(1))

        page.goto.side_effect = _goto
        page.content.side_effect = lambda: conteudo(estado["pag"])
        return page

    @contextmanager
    def _browser_fake(self, page):
        """Patcha iniciar_browser para ceder `page` — usado nos testes de fallback."""
        @contextmanager
        def _fake(*a, **kw):
            yield (page, Mock())

        with patch("apps.scrapers.scraper_mercadolivre.scraper.iniciar_browser", _fake):
            yield

    def test_le_os_cupons_do_dom_real_do_ml(self):
        """Caminho feliz: o HTTP traz o payload SSR e o browser nem é aberto."""
        from apps.scrapers.scraper_mercadolivre.scraper import mapear_cupons

        with open(self.DUMP, encoding="utf-8") as f:
            dados = json.load(f)
        # A 2ª página vem vazia: encerra o laço (o dump é de uma página só).
        vazio = {"appProps": {"pageProps": {"filteredCouponsData": {"coupons": []}}}}
        sess = self._http_falsa([dados, vazio])

        with patch("apps.scrapers.scraper_mercadolivre.scraper._ml_http_session", return_value=sess), \
                patch("apps.scrapers.scraper_mercadolivre.scraper.iniciar_browser") as browser:
            salvos = mapear_cupons()

        self.assertEqual(salvos, 30)                       # o dump tem 30 cupons
        self.assertEqual(Cupom.objects.count(), 30)
        browser.assert_not_called()                        # HTTP resolveu; sem Chromium
        cupom = Cupom.objects.get(campanha_id="13642210")
        self.assertIn("esquenta copa", cupom.titulo.lower())
        self.assertEqual(cupom.estado, "ativo")

    def test_pagina_vazia_nao_apaga_os_cupons_existentes(self):
        """Guarda anti-wipe: ML sem cupons não pode zerar o catálogo."""
        from apps.scrapers.scraper_mercadolivre.scraper import mapear_cupons

        Cupom.objects.create(campanha_id="999", titulo="Cupom antigo", estado="ativo",
                             valor_desconto=10.0, valor_minimo=0.0)
        vazio = {"appProps": {"pageProps": {"filteredCouponsData": {"coupons": []}}}}
        sess = self._http_falsa([vazio])

        with patch("apps.scrapers.scraper_mercadolivre.scraper._ml_http_session", return_value=sess):
            salvos = mapear_cupons()

        self.assertEqual(salvos, 0)
        self.assertEqual(Cupom.objects.get(campanha_id="999").estado, "ativo")

    def test_le_o_payload_tambem_no_formato_sem_appProps(self):
        """O ML alterna entre o payload aninhado em appProps.pageProps e o achatado.

        O extractor sempre aceitou os dois, mas devolvia a RAIZ e quem consumia só
        sabia descer por appProps: no formato achatado a extração dava certo, a lista
        vinha vazia, e a raspagem terminava em zero cupom sem dizer por quê.
        """
        from apps.scrapers.scraper_mercadolivre.scraper import mapear_cupons

        with open(self.DUMP, encoding="utf-8") as f:
            dump = json.load(f)
        achatado = {"filteredCouponsData":
                    dump["appProps"]["pageProps"]["filteredCouponsData"]}
        sess = self._http_falsa([achatado, {"filteredCouponsData": {"coupons": []}}])

        with patch("apps.scrapers.scraper_mercadolivre.scraper._ml_http_session", return_value=sess):
            salvos = mapear_cupons()

        self.assertEqual(salvos, 30)
        self.assertEqual(Cupom.objects.count(), 30)

    def test_varredura_parcial_nao_expira_o_que_nao_chegou_a_ver(self):
        """Falhar na página 2 não é evidência de que o resto do catálogo morreu.

        O bloco de expiração rodava com a FATIA já coletada, então uma falha de rede
        no meio marcava todo o resto como expirado — e a aba Cupons esvaziava.
        """
        from apps.scrapers.scraper_mercadolivre.scraper import mapear_cupons

        Cupom.objects.create(campanha_id="fora-da-fatia", titulo="Cupom de outra página",
                             estado="ativo", valor_desconto=10.0, valor_minimo=0.0)
        with open(self.DUMP, encoding="utf-8") as f:
            dump = json.load(f)
        # Página 1 ok; da 2ª em diante o payload some (as tentativas falham).
        sess = self._http_falsa([dump, "<html>bloqueado</html>"])

        with patch("apps.scrapers.scraper_mercadolivre.scraper._ml_http_session", return_value=sess):
            salvos = mapear_cupons()

        self.assertEqual(salvos, 30)
        self.assertEqual(Cupom.objects.get(campanha_id="fora-da-fatia").estado, "ativo")

    def test_varredura_completa_expira_o_cupom_que_saiu_do_ar(self):
        """O contrapeso do teste acima: chegando ao fim, expirar é o certo."""
        from apps.scrapers.scraper_mercadolivre.scraper import mapear_cupons

        Cupom.objects.create(campanha_id="saiu-do-ar", titulo="Cupom morto",
                             estado="ativo", valor_desconto=10.0, valor_minimo=0.0)
        with open(self.DUMP, encoding="utf-8") as f:
            dump = json.load(f)
        vazio = {"appProps": {"pageProps": {"filteredCouponsData": {"coupons": []}}}}
        sess = self._http_falsa([dump, vazio])

        with patch("apps.scrapers.scraper_mercadolivre.scraper._ml_http_session", return_value=sess):
            mapear_cupons()

        self.assertEqual(Cupom.objects.get(campanha_id="saiu-do-ar").estado, "expirado")

    def test_fallback_para_o_browser_quando_http_nao_traz_payload(self):
        """HTTP sem payload na 1ª página (challenge do ML) => abre o browser e conclui."""
        from apps.scrapers.scraper_mercadolivre.scraper import mapear_cupons

        with open(self.DUMP, encoding="utf-8") as f:
            dump = json.load(f)
        vazio = {"appProps": {"pageProps": {"filteredCouponsData": {"coupons": []}}}}
        # HTTP devolve sempre uma página de login (sem filteredCouponsData).
        sess = self._http_falsa(["<html>login</html>"])
        page = self._browser_page([dump, vazio])

        with patch("apps.scrapers.scraper_mercadolivre.scraper._ml_http_session", return_value=sess), \
                self._browser_fake(page):
            salvos = mapear_cupons()

        self.assertEqual(salvos, 30)
        self.assertEqual(Cupom.objects.count(), 30)

    def test_sessao_expirada_no_fallback_propaga(self):
        """Se o fallback abre o browser e a sessão caiu, SessaoExpirada sobe — é o que
        o SSE transforma no aviso de reconexão."""
        from apps.scrapers.scraper_mercadolivre.scraper import mapear_cupons
        from apps.scrapers.auxiliar import SessaoExpirada

        sess = self._http_falsa(["<html>login</html>"])

        @contextmanager
        def _fake(*a, **kw):
            raise SessaoExpirada("sessão caiu")
            yield  # pragma: no cover

        with patch("apps.scrapers.scraper_mercadolivre.scraper._ml_http_session", return_value=sess), \
                patch("apps.scrapers.scraper_mercadolivre.scraper.iniciar_browser", _fake):
            with self.assertRaises(SessaoExpirada):
                mapear_cupons()

    def test_trava_de_max_paginas_para_o_laco_sem_expirar(self):
        """Payload que nunca esvazia não pode rodar para sempre nem expirar o resto."""
        from apps.scrapers.scraper_mercadolivre.scraper import mapear_cupons

        Cupom.objects.create(campanha_id="de-outra-pagina", titulo="Não visto",
                             estado="ativo", valor_desconto=10.0, valor_minimo=0.0)

        def _get(url, **kw):
            # Cada página traz um cupom NOVO e nunca vem vazia -> força a trava.
            pag = int(re.search(r"page=(\d+)", url).group(1))
            payload = {"appProps": {"pageProps": {"filteredCouponsData": {
                "coupons": [{"campaignId": f"inf-{pag}",
                             "title": {"text": f"Cupom {pag}"},
                             "action": {"type": "button"}}]}}}}
            resp = Mock()
            resp.text = self._envelope(payload)
            resp.raise_for_status = Mock()
            return resp

        sess = Mock()
        sess.get.side_effect = _get

        with patch("apps.scrapers.scraper_mercadolivre.scraper._ml_http_session", return_value=sess):
            mapear_cupons()

        # Parou na trava (MAX_PAGINAS=200), sem varredura completa: nada é expirado.
        self.assertEqual(Cupom.objects.filter(campanha_id__startswith="inf-").count(), 200)
        self.assertEqual(Cupom.objects.get(campanha_id="de-outra-pagina").estado, "ativo")

    def test_para_na_ultima_pagina_via_pagination_total(self):
        """O payload traz `pagination.total`: a varredura para nele (fim natural) sem
        depender da página vazia seguinte, e expira o que saiu do ar."""
        from apps.scrapers.scraper_mercadolivre.scraper import mapear_cupons

        Cupom.objects.create(campanha_id="saiu-do-ar", titulo="Cupom morto",
                             estado="ativo", valor_desconto=10.0, valor_minimo=0.0)

        def _get(url, **kw):
            pag = int(re.search(r"page=(\d+)", url).group(1))
            payload = {"appProps": {"pageProps": {"filteredCouponsData": {
                "coupons": [{"campaignId": f"p{pag}",
                             "title": {"text": f"Cupom {pag}"},
                             "action": {"type": "button"}}],
                "pagination": {"total": 3}}}}}
            resp = Mock()
            resp.text = self._envelope(payload)
            resp.raise_for_status = Mock()
            return resp

        sess = Mock()
        sess.get.side_effect = _get

        with patch("apps.scrapers.scraper_mercadolivre.scraper._ml_http_session", return_value=sess):
            salvos = mapear_cupons()

        # Exatamente 3 páginas (p1..p3) e nada além; varredura completa -> expira.
        self.assertEqual(salvos, 3)
        self.assertEqual(Cupom.objects.filter(campanha_id__startswith="p").count(), 3)
        self.assertFalse(Cupom.objects.filter(campanha_id="p4").exists())
        self.assertEqual(Cupom.objects.get(campanha_id="saiu-do-ar").estado, "expirado")


class ProjecaoCatalogoCuponsTests(TestCase):
    """A aba Cupons lê só o CupomNormalizado. A projeção Cupom→CupomNormalizado
    rodava apenas no loop automático; a raspagem manual enchia a tabela Cupom e a
    aba seguia vazia."""

    def test_projeta_ativos_expira_ausentes_e_preserva_checkout(self):
        from apps.scrapers.scraper_mercadolivre.scraper import projetar_catalogo_cupons

        Cupom.objects.create(campanha_id="111", titulo="Cupom A", estado="ativo",
                             tipo_desconto="fixo", valor_desconto=10.0, valor_minimo=0.0)
        Cupom.objects.create(campanha_id="222", titulo="Cupom B", estado="ativo",
                             tipo_desconto="percentual", valor_desconto=15.0,
                             valor_minimo=50.0)
        Cupom.objects.create(campanha_id="333", titulo="Cupom vencido",
                             estado="expirado", valor_desconto=5.0, valor_minimo=0.0)
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web", defaults={
                "marketplace": "mercadolivre",
                "nome": "Mercado Livre — páginas públicas"})
        # Projeção antiga de uma campanha que saiu do ar + cupom de checkout,
        # que a sincronização de campanhas nunca pode tocar.
        CupomNormalizado.objects.create(
            fonte=fonte, external_id="campanha:999", marketplace="mercadolivre",
            titulo="Campanha antiga", link="https://x", estado="ativo")
        CupomNormalizado.objects.create(
            fonte=fonte, external_id="checkout:MEU10", marketplace="mercadolivre",
            titulo="Código de checkout", link="https://x", estado="ativo")

        projetados = projetar_catalogo_cupons()

        self.assertEqual(projetados, 2)
        ativos = set(CupomNormalizado.objects.filter(estado="ativo")
                     .values_list("external_id", flat=True))
        self.assertEqual(ativos, {"campanha:111", "campanha:222", "checkout:MEU10"})
        self.assertEqual(CupomNormalizado.objects.get(
            external_id="campanha:999").estado, "expirado")

    def test_sem_cupom_ativo_preserva_o_catalogo(self):
        """Anti-wipe: coleta caída não pode zerar a aba Cupons."""
        from apps.scrapers.scraper_mercadolivre.scraper import projetar_catalogo_cupons

        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web", defaults={
                "marketplace": "mercadolivre",
                "nome": "Mercado Livre — páginas públicas"})
        CupomNormalizado.objects.create(
            fonte=fonte, external_id="campanha:111", marketplace="mercadolivre",
            titulo="Segue no ar", link="https://x", estado="ativo")

        self.assertEqual(projetar_catalogo_cupons(), 0)
        self.assertEqual(CupomNormalizado.objects.get(
            external_id="campanha:111").estado, "ativo")


class DescartesDaRaspagemTests(SimpleTestCase):
    """Os motivos de descarte moravam em `continue` mudos e num logger.debug que o
    LOGGING em INFO apaga em produção. Um seletor renomeado zerava a coleta e o
    único sinal era o total — que só cai quando TUDO quebra de uma vez."""

    def test_card_perdido_por_seletor_sobe_para_warning(self):
        from apps.scrapers.scraper_mercadolivre import ofertas_scraper

        with self.assertLogs("apps.scrapers.scraper_mercadolivre.ofertas_scraper",
                             level="WARNING") as logs:
            ofertas_scraper._logar_descartes(
                100, 60, {"sem_nome_ou_link": 30, "sem_desconto": 10,
                          "preco_invalido": 0, "erro_no_card": 0})

        self.assertIn("100 lidos", logs.output[0])
        self.assertIn("sem nome ou link", logs.output[0])

    def test_descarte_normal_fica_em_info(self):
        """Card sem desconto é o trabalho normal da função, não um alerta."""
        from apps.scrapers.scraper_mercadolivre import ofertas_scraper

        with self.assertLogs("apps.scrapers.scraper_mercadolivre.ofertas_scraper",
                             level="INFO") as logs:
            ofertas_scraper._logar_descartes(
                100, 60, {"sem_nome_ou_link": 0, "sem_desconto": 40,
                          "preco_invalido": 0, "erro_no_card": 0})

        self.assertTrue(logs.output[0].startswith("INFO"))


class AfiliacaoPorMarketplaceTests(TestCase):
    """O badge da tela Promoções pergunta à loja se o item comissiona (can_affiliate).

    Antes, a view só conhecia a regra do ML e todo item Amazon exibia 'pendente'
    para sempre, mesmo com a tag salva e o link montável sem rede.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("afiliado-user", password="test")
        self.user.perfil.marcar_verificado()
        self.user.perfil.afiliado_tag_amazon = "minhaloja-20"
        self.user.perfil.save(update_fields=["afiliado_tag_amazon"])

    def _produto_amazon(self):
        return Produto.objects.create(
            marketplace="amazon", owner=self.user, asin="B0AFILIADO",
            nome="Cafeteira", origem="oferta", preco_sem_desconto=200,
            preco_com_cupom=100, link_produto="https://www.amazon.com.br/dp/B0AFILIADO",
        )

    def test_amazon_item_comissiona_quando_o_perfil_tem_tag(self):
        produto = self._produto_amazon()

        self.assertTrue(get_marketplace("amazon").can_affiliate(produto, self.user))

    def test_amazon_item_nao_comissiona_sem_tag_no_perfil(self):
        produto = self._produto_amazon()
        outro = get_user_model().objects.create_user("sem-tag", password="test")

        self.assertFalse(get_marketplace("amazon").can_affiliate(produto, outro))

    def test_mercadolivre_depende_do_link_pre_gerado(self):
        produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Fone", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=50,
            link_produto="https://example.com/fone",
        )
        mp = get_marketplace("mercadolivre")

        self.assertFalse(mp.can_affiliate(produto, self.user))

        produto.link_afiliado = "https://mercadolivre.com/sec/abc123"
        self.assertTrue(mp.can_affiliate(produto, self.user))

    def _produto_ml(self, nome="Fone"):
        return Produto.objects.create(
            marketplace="mercadolivre", nome=nome, origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=50,
            link_produto="https://example.com/fone",
        )

    def test_mercadolivre_conta_o_link_do_proprio_usuario(self):
        # O bug: can_affiliate lia só o Produto.link_afiliado (global), enquanto o
        # fluxo multi-tenant grava em LinkAfiliadoUsuario. Link gerado e funcionando
        # aparecia como "pendente", e o Link Builder era reaberto a cada envio.
        produto = self._produto_ml()
        mp = get_marketplace("mercadolivre")
        self.assertFalse(mp.can_affiliate(produto, self.user))

        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto, afiliado_ok=True,
            link_afiliado="https://mercadolivre.com/sec/meu-link",
        )

        self.assertTrue(mp.can_affiliate(produto, self.user))

    def test_mercadolivre_nao_conta_o_link_de_outro_usuario(self):
        # Cada um afilia com a conta dele: o link do vizinho não comissiona pra mim.
        produto = self._produto_ml()
        vizinho = get_user_model().objects.create_user("vizinho", password="test")
        LinkAfiliadoUsuario.objects.create(
            usuario=vizinho, produto=produto, afiliado_ok=True,
            link_afiliado="https://mercadolivre.com/sec/link-do-vizinho",
        )

        self.assertFalse(get_marketplace("mercadolivre").can_affiliate(produto, self.user))

    def test_tela_promocoes_mostra_link_do_usuario_como_pronto(self):
        produto = self._produto_ml("Fone com link")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto, afiliado_ok=True,
            link_afiliado="https://mercadolivre.com/sec/meu-link",
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("scraper-top"), {"loja": "mercadolivre"})

        listados = {p.id: p for p in response.context["produtos"]}
        self.assertTrue(listados[produto.id].afiliado_pronto)

    def test_tela_promocoes_mostra_resumo_e_ultimo_erro_da_afiliacao(self):
        pronto = self._produto_ml("Fone pronto")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=pronto, estado="pronto",
            link_afiliado="https://meli.la/pronto", afiliado_ok=True)
        falhou = self._produto_ml("Fone falhou")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=falhou, estado="erro",
            ultimo_erro="O Link Builder recusou a URL.",
            ultima_tentativa=timezone.now())
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("scraper-top"), {"loja": "mercadolivre"})

        self.assertEqual(response.context["afiliacao"]["prontos"], 1)
        self.assertEqual(response.context["afiliacao"]["erro"], 1)
        self.assertContains(response, "Afiliação: 1 prontos")
        self.assertContains(response, "1 com erro")
        self.assertContains(response, "O Link Builder recusou a URL.")

    def test_tela_promocoes_resolve_afiliacao_em_lote(self):
        # preparar_exibicao existe pra isto: uma query por página, não por produto.
        # Sem o lote, corrigir o badge trocaria o bug por 20 queries por load.
        for i in range(5):
            LinkAfiliadoUsuario.objects.create(
                usuario=self.user, produto=self._produto_ml(f"Fone {i}"),
                link_afiliado=f"https://mercadolivre.com/sec/l{i}", afiliado_ok=True,
            )
        self.client.force_login(self.user)

        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse("scraper-top"), {"loja": "mercadolivre"})

        self.assertEqual(len(response.context["produtos"]), 5)
        consultas_dos_badges = [
            q for q in ctx.captured_queries
            if (
                'SELECT "scrapers_linkafiliadousuario"."produto_id"' in q["sql"]
                and '"scrapers_linkafiliadousuario"."estado"' in q["sql"]
                and '"scrapers_linkafiliadousuario"."tentativas"' in q["sql"]
            )
        ]
        self.assertEqual(len(consultas_dos_badges), 1, consultas_dos_badges)

    def test_tela_promocoes_marca_item_amazon_como_pronto_sem_gravar_no_banco(self):
        produto = self._produto_amazon()
        self.client.force_login(self.user)

        response = self.client.get(reverse("scraper-top"), {"loja": "amazon"})

        listados = {p.id: p for p in response.context["produtos"]}
        self.assertTrue(listados[produto.id].afiliado_pronto)
        # A visita é um GET: nada de escrita no campo persistido.
        produto.refresh_from_db()
        self.assertFalse(produto.afiliado_ok)


class ParserDeNumeroDeRelatorioTests(SimpleTestCase):
    """Os portais são pt-BR e devolvem texto formatado.

    float() direto lia 'R$ 1.234,56' como 0.0 — e o sync gravava status "ok" do
    mesmo jeito, então o dashboard exibia R$ 0,00 com selo verde de "sincronizado".
    """

    def test_le_moeda_brasileira(self):
        from apps.scrapers.relatorios import _num

        self.assertEqual(_num("R$ 1.234,56"), 1234.56)
        self.assertEqual(_num("1.234,56"), 1234.56)
        self.assertEqual(_num("12,50"), 12.5)
        self.assertEqual(_num("R$ 0,00"), 0.0)

    def test_le_milhar_sem_decimal(self):
        from apps.scrapers.relatorios import _num

        # '1.234' cliques é mil duzentos e trinta e quatro, não 1,234.
        self.assertEqual(_num("1.234"), 1234)
        self.assertEqual(_num("12.345.678"), 12345678)

    def test_le_numero_cru_e_percentual(self):
        from apps.scrapers.relatorios import _num

        self.assertEqual(_num(1234.56), 1234.56)
        self.assertEqual(_num("42"), 42)
        self.assertEqual(_num("3,2%"), 3.2)
        self.assertEqual(_num("-15,00"), -15.0)

    def test_celula_sem_numero_vira_zero(self):
        from apps.scrapers.relatorios import _num

        for vazio in ("", None, "—", "n/d", "-"):
            self.assertEqual(_num(vazio), 0.0, vazio)


class _FakeLocator:
    """Mínimo do contrato do Playwright que _extract_table_rows usa."""

    def __init__(self, itens):
        self._itens = itens

    def count(self):
        return len(self._itens)

    def nth(self, i):
        return self._itens[i]

    def inner_text(self, timeout=None):
        return self._itens


class _FakeCelula:
    def __init__(self, texto):
        self._texto = texto

    def inner_text(self, timeout=None):
        return self._texto


class _FakeLinha:
    def __init__(self, celulas):
        self._celulas = [_FakeCelula(c) for c in celulas]

    def locator(self, seletor):
        return _FakeLocator(self._celulas)


class _FakePage:
    def __init__(self, linhas, tem_senha=False):
        self._linhas = [_FakeLinha(l) for l in linhas]
        self._tem_senha = tem_senha

    def locator(self, seletor):
        if "password" in seletor:
            return _FakeLocator([1] if self._tem_senha else [])
        return _FakeLocator(self._linhas)


class ExtracaoDeRelatorioTests(TestCase):
    """_extract_table_rows era o ponto cego: o teste de idempotência montava
    ReportRow na mão e pulava justamente a função onde os bugs moravam."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("relator", password="test")

    def _extrair(self, linhas, desde=None, ate=None):
        from datetime import date
        from apps.scrapers.relatorios import _extract_table_rows

        return _extract_table_rows(
            _FakePage(linhas), "mercadolivre",
            desde or date(2026, 7, 1), ate or date(2026, 7, 15))

    def test_le_a_tabela_em_formato_brasileiro(self):
        linhas = self._extrair([["grupo-casa", "Fone JBL", "1.234", "12", "R$ 1.999,90", "R$ 199,99"]])

        self.assertEqual(len(linhas), 1)
        self.assertEqual(linhas[0].cliques, 1234)
        self.assertEqual(linhas[0].pedidos, 12)
        self.assertEqual(linhas[0].receita, 1999.90)
        self.assertEqual(linhas[0].comissao, 199.99)

    def test_tabela_sem_numero_reconhecido_falha_em_vez_de_reportar_zero(self):
        from apps.scrapers.relatorios import ReportSyncError

        # Achar a tabela e não entender número nenhum é parser errado, não conta
        # zerada. Reportar "ok" aqui é o que produzia R$ 0,00 com selo verde.
        with self.assertRaises(ReportSyncError):
            self._extrair([["grupo", "Fone", "n/d", "n/d", "n/d", "n/d"]])

    def test_sessao_expirada_pede_acao(self):
        from datetime import date
        from apps.scrapers.relatorios import ReportSyncActionRequired, _extract_table_rows

        with self.assertRaises(ReportSyncActionRequired):
            _extract_table_rows(_FakePage([], tem_senha=True), "mercadolivre",
                                date(2026, 7, 1), date(2026, 7, 15))


class ResumoFinanceiroTests(TestCase):
    """O dashboard somava snapshots sobrepostos e inflava a receita ~30x."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("dono-receita", password="test")

    def _snapshot(self, dia, comissao, marketplace="mercadolivre", etiqueta="grupo"):
        from datetime import date, timedelta as td

        return ReceitaAfiliado.objects.create(
            usuario=self.user, marketplace=marketplace, data=dia,
            etiqueta=etiqueta, pedidos=2, receita=comissao * 10, comissao=comissao,
            cliques=100, periodo_inicio=dia - td(days=14), periodo_fim=dia,
            granularidade="etiqueta", origem="auto",
            hash_origem=f"{marketplace}-{dia}-{etiqueta}",
        )

    def test_snapshots_de_dias_diferentes_nao_se_somam(self):
        from datetime import date
        from apps.scrapers.relatorios import resumo_financeiro

        # Cada sync grava o acumulado dos últimos 14 dias carimbado com a data de
        # hoje. Três dias de sync = quase a mesma comissão três vezes no banco.
        self._snapshot(date(2026, 7, 13), 100.0)
        self._snapshot(date(2026, 7, 14), 110.0)
        self._snapshot(date(2026, 7, 15), 120.0)

        resumo = resumo_financeiro(self.user)

        # Só o mais recente, não 330.
        self.assertEqual(resumo["comissao"], 120.0)
        self.assertEqual(resumo["periodo_fim"], date(2026, 7, 15))

    def test_linhas_do_mesmo_snapshot_se_somam(self):
        from datetime import date
        from apps.scrapers.relatorios import resumo_financeiro

        # Dentro de um snapshot as linhas são fatias distintas (por etiqueta): aí
        # somar é o certo.
        self._snapshot(date(2026, 7, 15), 50.0, etiqueta="grupo-casa")
        self._snapshot(date(2026, 7, 15), 30.0, etiqueta="grupo-tech")

        self.assertEqual(resumo_financeiro(self.user)["comissao"], 80.0)

    def test_soma_o_ultimo_snapshot_de_cada_loja(self):
        from datetime import date
        from apps.scrapers.relatorios import resumo_financeiro

        # Lojas sincronizam em dias diferentes: cada uma contribui com o seu último.
        self._snapshot(date(2026, 7, 15), 120.0, marketplace="mercadolivre")
        self._snapshot(date(2026, 7, 10), 40.0, marketplace="amazon")

        self.assertEqual(resumo_financeiro(self.user)["comissao"], 160.0)

    def test_sem_receita_nao_quebra(self):
        from apps.scrapers.relatorios import resumo_financeiro

        self.assertIsNone(resumo_financeiro(self.user)["comissao"])


class GeracaoDeLinksEmLoteTests(TestCase):
    """O worker que tira os produtos de 'pendente'.

    Nada em produção gerava link: não havia worker Celery, o beat_schedule é vazio e
    o endpoint de gerar links não é referenciado por template nenhum. Cada raspagem
    só empilhava mais "pendente" na tela de Promoções.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("linkeiro", password="test")

    def _produto(self, nome="Fone", **extra):
        return Produto.objects.create(
            marketplace="mercadolivre", nome=nome, origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=50,
            link_produto="https://example.com/fone", **extra)

    @patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=True)
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.prefetch_links")
    def test_gera_para_os_pendentes_do_usuario(self, prefetch, _conectado):
        prefetch.return_value = (1, 0)
        produto = self._produto()

        res = _rodar_links(lote=40)

        prefetch.assert_called_once()
        enviados, kwargs = prefetch.call_args
        self.assertEqual([p.id for p in enviados[0]], [produto.id])
        self.assertEqual(kwargs["usuario"], self.user)
        self.assertEqual(res, {"gerados": 1, "falhas": 0, "pulados": 0})

    @patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=True)
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.prefetch_links")
    def test_nao_regera_o_que_o_usuario_ja_tem(self, prefetch, _conectado):
        prefetch.return_value = (1, 0)
        pronto = self._produto("Ja tenho")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=pronto, afiliado_ok=True,
            link_afiliado="https://mercadolivre.com/sec/abc")
        pendente = self._produto("Falta")

        _rodar_links(lote=40)

        enviados, _ = prefetch.call_args
        self.assertEqual([p.id for p in enviados[0]], [pendente.id])

    @patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=False)
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.prefetch_links")
    def test_pula_usuario_sem_sessao_ml(self, prefetch, _conectado):
        # Gerar link exige o Link Builder logado: sem sessão não há o que fazer.
        cache.clear()
        self._produto()

        self.assertEqual(_rodar_links(lote=40),
                         {"gerados": 0, "falhas": 0, "pulados": 1})
        prefetch.assert_not_called()

    @patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=True)
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.prefetch_links")
    def test_respeita_o_tamanho_do_lote(self, prefetch, _conectado):
        prefetch.return_value = (2, 0)
        for i in range(5):
            self._produto(f"Fone {i}")

        _rodar_links(lote=2)

        enviados, _ = prefetch.call_args
        self.assertEqual(len(enviados[0]), 2)

    def test_lote_permite_orm_so_durante_o_playwright_e_restaura_o_ambiente(self):
        produto = self._produto()
        anterior = os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)

        @contextmanager
        def browser_falso(**_kwargs):
            yield Mock(), Mock()

        try:
            with patch("apps.scrapers.scraper_mercadolivre.link.iniciar_browser", browser_falso), \
                 patch("apps.scrapers.scraper_mercadolivre.link._abrir_link_builder"), \
                 patch("apps.scrapers.scraper_mercadolivre.link._afiliar_url_na_pagina",
                       return_value="https://meli.la/link"):
                gerados, falhas = ml_link.gerar_links_em_lote([produto], usuario=self.user)
            self.assertEqual((gerados, falhas), (1, 0))
            self.assertTrue(LinkAfiliadoUsuario.objects.filter(
                usuario=self.user, produto=produto, link_afiliado="https://meli.la/link").exists())
            self.assertNotIn("DJANGO_ALLOW_ASYNC_UNSAFE", os.environ)
        finally:
            if anterior is not None:
                os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = anterior

    @patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=True)
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.prefetch_links",
           side_effect=RuntimeError("sessão expirada"))
    def test_falha_de_um_usuario_nao_derruba_o_ciclo(self, _prefetch, _conectado):
        # A sessão ML é de cada um: a do vizinho vencer não pode me impedir de gerar.
        self._produto()

        self.assertEqual(_rodar_links(lote=40),
                         {"gerados": 0, "falhas": 0, "pulados": 0})

    @patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=True)
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.prefetch_links")
    def test_item_nao_afiliavel_sai_da_fila(self, prefetch, _conectado):
        """A starvation do lote: sem sair da fila, um punhado de itens que nunca
        afiliam ocupa as 40 vagas a cada ciclo e nenhum outro produto avança."""
        prefetch.return_value = (0, 1)
        proibido = self._produto("Perfil de vendedor")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=proibido, estado="nao_afiliavel",
            ultimo_erro="Não é uma página de produto.")
        util = self._produto("Fone que afilia")

        _rodar_links(lote=40)

        enviados, _ = prefetch.call_args
        self.assertEqual([p.id for p in enviados[0]], [util.id])

    @patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=True)
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.prefetch_links")
    def test_backoff_segura_o_item_ate_a_proxima_tentativa(self, prefetch, _conectado):
        prefetch.return_value = (0, 0)
        produto = self._produto()
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto, estado="pendente", tentativas=1,
            proxima_tentativa=timezone.now() + timedelta(minutes=5))

        _rodar_links(lote=40)
        prefetch.assert_not_called()          # de castigo

        LinkAfiliadoUsuario.objects.update(
            proxima_tentativa=timezone.now() - timedelta(seconds=1))
        _rodar_links(lote=40)
        prefetch.assert_called_once()         # venceu, volta pra fila

    @patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=False)
    def test_usuario_sem_sessao_ml_vira_evento(self, _conectado):
        """Antes era um `continue` mudo: o usuário nunca gerava link e nada dizia
        por quê — nem o log, nem a tela."""
        cache.clear()
        self._produto()

        res = _rodar_links(lote=40)

        self.assertEqual(res["pulados"], 1)
        evento = EventoOperacional.objects.get(evento="links_sem_sessao")
        self.assertEqual(evento.usuario, self.user)
        self.assertEqual(evento.level, "warning")

    @patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=False)
    def test_aviso_de_sessao_tem_cooldown(self, _conectado):
        """Tick de 5min = 288 eventos/dia por usuário caído; a tela afogaria
        justamente no aviso que precisa ser lido."""
        cache.clear()
        self._produto()

        for _ in range(5):
            _rodar_links(lote=40)

        self.assertEqual(
            EventoOperacional.objects.filter(evento="links_sem_sessao").count(), 1)


class RegistroDeFalhaDeLinkTests(TestCase):
    """Todo item sem link precisa carregar o motivo.

    O gerador contava a falha e seguia (`falhas += 1; continue`), sem log nem
    registro: o produto ficava "pendente" para sempre e não havia uma única linha
    dizendo por quê. Era a origem mais provável da pilha que não saía.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("registrador", password="test")
        self.produto = Produto.objects.create(
            marketplace="mercadolivre", nome="X", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=50,
            link_produto="https://example.com/x")

    def test_falha_terminal_sai_da_fila_de_vez(self):
        from apps.scrapers.afiliado import registrar_falha

        registrar_falha(self.user, self.produto, "Catálogo sem item real", terminal=True)

        linha = LinkAfiliadoUsuario.objects.get(usuario=self.user, produto=self.produto)
        self.assertEqual(linha.estado, "nao_afiliavel")
        self.assertIsNone(linha.proxima_tentativa)
        self.assertIn("Catálogo", linha.ultimo_erro)

    def test_falha_transitoria_agenda_retry_com_backoff_crescente(self):
        from apps.scrapers.afiliado import registrar_falha

        registrar_falha(self.user, self.produto, "timeout")
        primeira = LinkAfiliadoUsuario.objects.get(produto=self.produto).proxima_tentativa
        registrar_falha(self.user, self.produto, "timeout")
        segunda = LinkAfiliadoUsuario.objects.get(produto=self.produto).proxima_tentativa

        self.assertGreater(segunda, primeira)
        self.assertEqual(LinkAfiliadoUsuario.objects.get(produto=self.produto).tentativas, 2)

    def test_desiste_depois_de_muitas_falhas(self):
        """Insistir para sempre não é resiliência: é o item ocupando a fila."""
        from apps.scrapers.afiliado import MAX_TENTATIVAS_ERRO, registrar_falha

        for _ in range(MAX_TENTATIVAS_ERRO):
            registrar_falha(self.user, self.produto, "o Link Builder recusou")

        linha = LinkAfiliadoUsuario.objects.get(produto=self.produto)
        self.assertEqual(linha.estado, "erro")
        self.assertIsNone(linha.proxima_tentativa)

    def test_item_com_link_ignora_falha_superveniente(self):
        from apps.scrapers.afiliado import registrar_falha

        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=self.produto, estado="pronto",
            link_afiliado="https://meli.la/ok")

        registrar_falha(self.user, self.produto, "ruído")

        linha = LinkAfiliadoUsuario.objects.get(produto=self.produto)
        self.assertEqual(linha.estado, "pronto")
        self.assertEqual(linha.ultimo_erro, "")

    def test_url_de_catalogo_e_recusada_com_motivo_legivel(self):
        from apps.scrapers.scraper_mercadolivre.link import _motivo_url_recusada

        motivo = _motivo_url_recusada("https://www.mercadolivre.com.br/up/MLBU123")

        self.assertIn("catálogo", motivo.lower())

    def test_listagem_distingue_na_fila_de_nao_afiliavel(self):
        from apps.scrapers.marketplaces.registry import get_marketplace

        outro = Produto.objects.create(
            marketplace="mercadolivre", nome="Y", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=50,
            link_produto="https://example.com/y")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=self.produto, estado="nao_afiliavel",
            ultimo_erro="Perfil, não produto.")

        produtos = [self.produto, outro]
        get_marketplace("mercadolivre").preparar_exibicao(produtos, usuario=self.user)

        self.assertEqual(self.produto.afiliado_estado, "nao_afiliavel")
        self.assertEqual(self.produto.afiliado_motivo, "Perfil, não produto.")
        self.assertEqual(outro.afiliado_estado, "pendente")


class PublicacaoOrfaTests(TestCase):
    """Publicacao nasce 'pendente' antes do trabalho; nada pode deixá-la assim."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("orfa-user", password="test")
        self.produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Fone", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=50,
            link_produto="https://example.com/fone",
            link_afiliado="https://mercadolivre.com/sec/abc",
        )

    def _publicacao(self, status="pendente", idade_horas=0):
        pub = Publicacao.objects.create(
            usuario=self.user, produto=self.produto, canal="whatsapp",
            destino_id="grupo@g.us", status=status,
        )
        if idade_horas:
            Publicacao.objects.filter(pk=pub.pk).update(
                criada_em=timezone.now() - timedelta(hours=idade_horas))
        return pub

    def test_pendente_antiga_e_fechada_como_falha(self):
        pub = self._publicacao(idade_horas=2)

        self.assertEqual(reconciliar_publicacoes_orfas(), 1)

        pub.refresh_from_db()
        self.assertEqual(pub.status, "falhou")
        self.assertIn("interrompido", pub.erro)

    def test_pendente_recente_e_um_envio_em_curso_e_nao_e_tocada(self):
        pub = self._publicacao()

        self.assertEqual(reconciliar_publicacoes_orfas(), 0)

        pub.refresh_from_db()
        self.assertEqual(pub.status, "pendente")

    def test_envio_concluido_antigo_nao_e_reescrito(self):
        pub = self._publicacao(status="enviado", idade_horas=5)

        reconciliar_publicacoes_orfas()

        pub.refresh_from_db()
        self.assertEqual(pub.status, "enviado")

    @patch("apps.scrapers.ofertas.montar_mensagem", side_effect=RuntimeError("boom"))
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.verify_affiliate_tag",
           return_value=True)
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.build_affiliate_link")
    def test_excecao_inesperada_fecha_a_publicacao_e_propaga(self, build, _tag, _msg):
        build.return_value = {
            "link_afiliado": "https://mercadolivre.com/sec/abc",
            "afiliado_ok": True, "url_isca": "https://example.com/fone",
        }

        with self.assertRaises(RuntimeError):
            ofertas.enviar_oferta_de_produto(
                self.produto, "grupo@g.us", verificar=False, usuario=self.user)

        pub = Publicacao.objects.get(usuario=self.user, produto=self.produto)
        self.assertEqual(pub.status, "falhou")
        self.assertIn("erro inesperado no envio", pub.erro)


class RelatorioSaudeTests(TestCase):
    """A tela de saúde é o que substitui a cliente como detector de falha.

    Os testes fixam as duas propriedades que a tornam confiável: agrupar sem perder
    gravidade, e nunca chamar de "saudável" um sistema que só está calado.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("saude-user", password="test")
        self.admin = get_user_model().objects.create_superuser(
            "saude-admin", "admin@x.com", "test")

    def _evento(self, evento, level="error", pipeline="publicacao", **kw):
        """Cria o evento e projeta o incidente, como log_event faz em produção.

        A projeção é do worker `monitor`; resumo() é só leitura. Antes o próprio
        resumo() reconciliava, e estes testes dependiam disso sem dizer.
        """
        from apps.scrapers.incidentes_saude import reconciliar_pendentes

        criado = EventoOperacional.objects.create(
            pipeline=pipeline, evento=evento, level=level,
            mensagem=kw.pop("mensagem", "falhou"), usuario=kw.pop("usuario", self.user),
            contexto=kw.pop("contexto", {}),
        )
        reconciliar_pendentes()
        return criado

    def test_agrupa_ocorrencias_repetidas_num_problema_so(self):
        from apps.scrapers.saude import resumo

        for _ in range(4):
            self._evento("send_failed", level="warning")

        r = resumo(horas=24)
        self.assertEqual(len(r["problemas"]), 1)
        self.assertEqual(r["problemas"][0]["n"], 4)
        self.assertEqual(r["problemas"][0]["usuarios"], 1)

    def test_relatorio_global_lista_todas_as_contas_afetadas(self):
        """Erros iguais de contas diferentes ficam no mesmo problema, sem omitir nomes."""
        from apps.scrapers.saude import resumo

        outra_conta = get_user_model().objects.create_user("outra-conta", password="test")
        self._evento("send_failed", level="warning", usuario=self.user)
        self._evento("send_failed", level="warning", usuario=outra_conta)

        problema = resumo(horas=24)["problemas"][0]

        self.assertEqual(problema["usuarios"], 2)
        # Ordem por username; cada conta traz o exemplo do próprio último erro.
        self.assertEqual(
            [(a["usuario_id"], a["usuario__username"]) for a in problema["afetados"]],
            [(outra_conta.id, "outra-conta"), (self.user.id, "saude-user")],
        )
        self.assertTrue(all(a["exemplo"] is not None for a in problema["afetados"]))

        self.client.force_login(self.admin)
        with patch("apps.scrapers.saude._workers", return_value=[]):
            resposta = self.client.get(reverse("superadmin-saude"))
        self.assertContains(resposta, "outra-conta")
        self.assertContains(
            resposta, reverse("superadmin-usuario", args=[outra_conta.id]))

    def test_saude_filtra_por_username(self):
        from apps.scrapers.saude import resumo

        outra_conta = get_user_model().objects.create_user("lules", password="test")
        self._evento("send_failed", level="warning", usuario=self.user)
        self._evento("send_timeout", level="error", pipeline="whatsapp", usuario=outra_conta)

        with patch("apps.scrapers.saude._workers", return_value=[]):
            r = resumo(horas=24, usuario=outra_conta)
        self.assertEqual([(p["pipeline"], p["evento"]) for p in r["problemas"]],
                         [("whatsapp", "send_timeout")])

        self.client.force_login(self.admin)
        with patch("apps.scrapers.saude._workers", return_value=[]):
            resposta = self.client.get(reverse("superadmin-saude"), {"usuario": "LuLeS"})
        self.assertContains(resposta, "Eventos de lules")
        self.assertNotContains(resposta, "saude-user")

    def test_evento_global_sem_conta_aparece_no_bucket_sistema(self):
        """`fonte_falhou` ("uma loja parou de responder") não tem usuário: é do sistema
        e não pode sumir da tela por não estar amarrado a uma conta."""
        from apps.scrapers.saude import resumo

        self._evento("fonte_falhou", level="error", pipeline="scraper",
                     mensagem="A coleta da loja mercadolivre falhou.",
                     contexto={"marketplace": "mercadolivre"}, usuario=None)

        problema = resumo(horas=24)["problemas"][0]
        self.assertEqual(problema["afetados"], [])
        self.assertIsNotNone(problema["sistema"])
        self.assertEqual(problema["sistema"].evento, "fonte_falhou")

        self.client.force_login(self.admin)
        with patch("apps.scrapers.saude._workers", return_value=[]):
            resposta = self.client.get(reverse("superadmin-saude"))
        self.assertContains(resposta, "Sistema (todas as contas)")
        self.assertContains(resposta, "A coleta da loja mercadolivre falhou.")

    def test_erro_vem_antes_de_aviso_mesmo_sendo_menos_frequente(self):
        from apps.scrapers.saude import resumo

        for _ in range(9):
            self._evento("send_failed", level="warning")
        self._evento("config_pausada", level="error")

        problemas = resumo(horas=24)["problemas"]
        self.assertEqual(problemas[0]["evento"], "config_pausada")
        self.assertEqual(problemas[1]["evento"], "send_failed")

    def test_evento_traduzido_para_linguagem_de_negocio(self):
        from apps.scrapers.saude import resumo

        self._evento("config_pausada")
        p = resumo(horas=24)["problemas"][0]
        self.assertEqual(p["titulo"], "Automação pausada sozinha")
        self.assertIn("parou de receber ofertas", p["significa"])
        self.assertTrue(p["acao"])

    def test_evento_nao_catalogado_nao_some_da_tela(self):
        from apps.scrapers.saude import resumo

        self._evento("evento_que_nao_existe_no_catalogo")
        p = resumo(horas=24)["problemas"][0]
        self.assertEqual(p["titulo"], "evento_que_nao_existe_no_catalogo")
        self.assertIn("não catalogado", p["significa"])

    def test_incidente_aberto_antigo_continua_aparecendo(self):
        """Aberto é problema de AGORA, não histórico: a janela não o esconde.

        Contrato de _incidentes: "abertos sempre aparecem; concluídos seguem o
        período". Este teste já afirmou o contrário, e passava só porque a projeção
        era preguiçosa e limitada à janela consultada — em produção, onde log_event
        projeta na hora, um incidente aberto de 48h sempre apareceu no filtro de 24h.
        """
        from apps.scrapers.models import IncidenteSaude
        from apps.scrapers.saude import resumo

        antigo = self._evento("config_pausada")
        ha_48h = timezone.now() - timedelta(hours=48)
        EventoOperacional.objects.filter(pk=antigo.pk).update(criado_em=ha_48h)
        IncidenteSaude.objects.update(primeira_ocorrencia=ha_48h, ultima_ocorrencia=ha_48h)

        self.assertEqual(len(resumo(horas=24)["problemas"]), 1)
        self.assertEqual(len(resumo(horas=168)["problemas"]), 1)

    def test_concluido_fora_da_janela_some_da_tela(self):
        """Concluído é histórico: some quando sai do período escolhido."""
        from apps.scrapers.models import IncidenteSaude
        from apps.scrapers.saude import resumo

        self._evento("config_pausada")
        ha_48h = timezone.now() - timedelta(hours=48)
        IncidenteSaude.objects.update(status="concluido", confirmado_em=ha_48h,
                                      confirmacao="resolvido")

        self.assertEqual(resumo(horas=24)["problemas"], [])
        self.assertEqual(resumo(horas=24)["concluidos"], [])
        self.assertEqual(len(resumo(horas=168)["concluidos"]), 1)

    def test_sem_erro_e_com_worker_saudavel_o_veredito_e_ok(self):
        from apps.scrapers.saude import resumo

        with patch("apps.scrapers.saude._workers", return_value=[]):
            r = resumo(horas=24)
        self.assertEqual(r["estado"], "ok")

    def test_silencio_com_worker_parado_nao_e_saude(self):
        """Zero erro porque nada rodou é o pior falso negativo possível."""
        from apps.scrapers.saude import resumo

        parado = [{"job": "envio", "nome": "Envio", "ligado": True, "vivo": False,
                   "fase": "?", "ultima_msg": "", "alerta": True}]
        with patch("apps.scrapers.saude._workers", return_value=parado):
            r = resumo(horas=24)

        self.assertEqual(r["estado"], "critico")
        self.assertIn("não está rodando", r["texto"])

    def test_pagina_exige_superadmin(self):
        self.client.force_login(self.user)
        resposta = self.client.get(reverse("superadmin-saude"))
        self.assertNotEqual(resposta.status_code, 200)

    def test_pagina_renderiza_para_superadmin(self):
        self._evento("config_pausada")
        self.client.force_login(self.admin)
        with patch("apps.scrapers.saude._workers", return_value=[]):
            resposta = self.client.get(reverse("superadmin-saude"))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Automação pausada sozinha")
        self.assertContains(resposta, "Visão geral: avisos e erros de todas as contas")


class RetesteDaSaudeTests(TestCase):
    """A tela precisa conseguir baixar o próprio vermelho.

    O botão só aparecia para grupos com EXATAMENTE 1 incidente e causas
    whatsapp_/link_/sync_/email_ — ou seja, sumia justamente no caso que mais
    importa (o mesmo problema em várias contas), e conexão/scraper não tinham
    reteste nenhum. O resultado era uma pilha de erros que ninguém conseguia fechar.
    """

    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            "reteste-admin", "a@x.com", "test")
        self.u1 = get_user_model().objects.create_user("conta-1", password="test")
        self.u2 = get_user_model().objects.create_user("conta-2", password="test")
        self.client.force_login(self.admin)

    def _incidente(self, usuario, causa="conexao_caiu", pipeline="conexao",
                   escopo="servico:WhatsApp", **kw):
        from apps.scrapers.models import IncidenteSaude
        return IncidenteSaude.objects.create(
            chave=uuid.uuid4().hex, causa=causa, pipeline=pipeline, escopo=escopo,
            usuario=usuario, level=kw.pop("level", "error"), status="aberto",
            primeira_ocorrencia=timezone.now(), ultima_ocorrencia=timezone.now(),
            ultima_mensagem="caiu", contexto=kw.pop("contexto", {"servico": "WhatsApp"}),
        )

    def test_reteste_fecha_o_grupo_inteiro_nao_so_um(self):
        from apps.scrapers.models import IncidenteSaude

        a = self._incidente(self.u1)
        self._incidente(self.u2)

        with patch("apps.scrapers.conexoes.estado_whatsapp",
                   return_value=_estado_conectado("WhatsApp")):
            self.client.post(reverse("superadmin-saude-retestar", args=[a.pk]))

        self.assertEqual(IncidenteSaude.objects.filter(status="aberto").count(), 0)
        self.assertEqual(IncidenteSaude.objects.filter(status="concluido").count(), 2)

    def test_conexao_de_pe_agora_conclui_o_incidente(self):
        """A causa nº1 de 'Saúde vermelha, dashboard verde'."""
        from apps.scrapers.models import IncidenteSaude

        inc = self._incidente(self.u1)

        with patch("apps.scrapers.conexoes.estado_whatsapp",
                   return_value=_estado_conectado("WhatsApp")):
            self.client.post(reverse("superadmin-saude-retestar", args=[inc.pk]))

        inc.refresh_from_db()
        self.assertEqual(inc.status, "concluido")
        self.assertIn("conectado", inc.confirmacao.lower())

    def test_conexao_ainda_caida_mantem_aberto_com_o_motivo(self):
        inc = self._incidente(self.u1)

        with patch("apps.scrapers.conexoes.estado_whatsapp",
                   return_value=_estado_caido("WhatsApp", "WhatsApp não está pareado.")):
            r = self.client.post(reverse("superadmin-saude-retestar", args=[inc.pk]),
                                 follow=True)

        inc.refresh_from_db()
        self.assertEqual(inc.status, "aberto")
        self.assertIn("não está pareado", " ".join(str(m) for m in get_messages(r.wsgi_request)))

    def test_grupo_parcial_avisa_quantas_faltam(self):
        """Uma conta voltar não pode dar 'tudo certo' quando a outra segue caída."""
        from apps.scrapers.models import IncidenteSaude

        a = self._incidente(self.u1)
        self._incidente(self.u2)
        estados = {self.u1: _estado_conectado("WhatsApp"),
                   self.u2: _estado_caido("WhatsApp", "ainda fora")}

        with patch("apps.scrapers.conexoes.estado_whatsapp",
                   side_effect=lambda u, **k: estados[u]):
            self.client.post(reverse("superadmin-saude-retestar", args=[a.pk]))

        self.assertEqual(IncidenteSaude.objects.filter(status="aberto").count(), 1)
        self.assertEqual(IncidenteSaude.objects.filter(status="concluido").count(), 1)

    def test_reteste_preserva_o_filtro(self):
        """O redirect nu devolvia o superadmin para 24h/global, perdendo a conta que
        ele estava investigando."""
        inc = self._incidente(self.u1)

        with patch("apps.scrapers.conexoes.estado_whatsapp",
                   return_value=_estado_conectado("WhatsApp")):
            r = self.client.post(reverse("superadmin-saude-retestar", args=[inc.pk]),
                                 {"horas": "168", "usuario": "conta-1"})

        self.assertIn("horas=168", r.url)
        self.assertIn("usuario=conta-1", r.url)

    def test_conta_sem_perfil_nao_vira_reteste_falhou_generico(self):
        """Perfil.DoesNotExist era capturado pelo except genérico e virava
        'Reteste falhou', escondendo a causa real."""
        from apps.scrapers.views_admin import _retestar_incidente

        inc = self._incidente(self.u1, causa="whatsapp_confirmacao",
                              pipeline="publicacao", escopo="whatsapp:123@g.us")
        Perfil = self.u1.perfil.__class__
        Perfil.objects.filter(user=self.u1).delete()
        self.u1.refresh_from_db()

        r = _retestar_incidente(inc)

        self.assertFalse(r["sucesso"])
        self.assertIn("perfil", r["mensagem"].lower())

    def test_causas_de_conexao_e_scraper_agora_sao_retestaveis(self):
        from apps.scrapers.saude import _retestavel

        for causa in ("conexao_caiu", "scrape_erro", "fonte_falhou", "cupons_vazios",
                      "links_sem_sessao", "whatsapp_confirmacao", "sync_failed"):
            self.assertTrue(_retestavel(causa), causa)
        self.assertFalse(_retestavel("signup"))


def _estado_conectado(servico):
    from apps.scrapers.conexoes import Estado
    return Estado(True, servico, "sonda", "", "", timezone.now())


def _estado_caido(servico, motivo):
    from apps.scrapers.conexoes import Estado
    return Estado(False, servico, "sonda", motivo, "sem_pareamento", timezone.now())


class AutoRefreshDaSaudeTests(TestCase):
    """O endpoint de polling. Só pode existir porque resumo() virou só leitura."""

    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            "json-admin", "a@x.com", "test")
        self.client.force_login(self.admin)

    def test_json_responde_o_resumo(self):
        r = self.client.get(reverse("superadmin-saude-json"))

        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn(d["estado"], ("ok", "atencao", "critico"))
        self.assertIn("assinatura", d)
        self.assertTrue(d["workers"])

    def test_polling_nao_infla_ocorrencias(self):
        """A regressão que o auto-refresh podia introduzir: resumo() escrevendo no
        GET faria cada aba reprocessar o lote a cada 15s."""
        from apps.scrapers.incidentes_saude import reconciliar_pendentes
        from apps.scrapers.models import IncidenteSaude

        user = get_user_model().objects.create_user("pollado", password="x")
        EventoOperacional.objects.create(
            pipeline="publicacao", evento="send_failed", level="error",
            mensagem="falhou", usuario=user, contexto={"canal": "whatsapp",
                                                       "destino": "1@g.us"})
        reconciliar_pendentes()

        for _ in range(10):
            self.client.get(reverse("superadmin-saude-json"))

        self.assertEqual(IncidenteSaude.objects.get(usuario=user).ocorrencias, 1)

    def test_json_e_so_para_superadmin(self):
        self.client.force_login(get_user_model().objects.create_user("zé", password="x"))

        r = self.client.get(reverse("superadmin-saude-json"))

        self.assertNotEqual(r.status_code, 200)


class IncidenteDeConexaoOrfaoTests(TestCase):
    """Incidente aberto por um watchdog que morreu antes de registrar a queda no
    Perfil não tem transição futura para fechá-lo: ficaria vermelho para sempre.
    É a pilha de erros antigos da tela que ninguém conseguia baixar."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("orfao", password="x")

    def test_fecha_incidente_cuja_conexao_esta_de_pe(self):
        from apps.scrapers.incidentes_saude import fechar_conexoes_restabelecidas
        from apps.scrapers.models import IncidenteSaude

        inc = IncidenteSaude.objects.create(
            chave=uuid.uuid4().hex, causa="conexao_caiu", pipeline="conexao",
            escopo="servico:WhatsApp", usuario=self.user, level="error",
            status="aberto", primeira_ocorrencia=timezone.now(),
            ultima_ocorrencia=timezone.now(), ultima_mensagem="caiu",
            contexto={"servico": "WhatsApp"})

        with patch("apps.scrapers.conexoes.estado_whatsapp",
                   return_value=_estado_conectado("WhatsApp")):
            self.assertEqual(fechar_conexoes_restabelecidas(), 1)

        inc.refresh_from_db()
        self.assertEqual(inc.status, "concluido")

    def test_nao_fecha_o_que_segue_caido(self):
        from apps.scrapers.incidentes_saude import fechar_conexoes_restabelecidas
        from apps.scrapers.models import IncidenteSaude

        IncidenteSaude.objects.create(
            chave=uuid.uuid4().hex, causa="conexao_caiu", pipeline="conexao",
            escopo="servico:Mercado Livre", usuario=self.user, level="error",
            status="aberto", primeira_ocorrencia=timezone.now(),
            ultima_ocorrencia=timezone.now(), ultima_mensagem="caiu",
            contexto={"servico": "Mercado Livre"})

        with patch("apps.scrapers.conexoes.estado_ml",
                   return_value=_estado_caido("Mercado Livre", "sessão expirou")):
            self.assertEqual(fechar_conexoes_restabelecidas(), 0)


class CatalogoDaSaudeTests(SimpleTestCase):
    def test_toda_causa_gerada_tem_traducao(self):
        """whatsapp_timeout_entrega era gerado mas não catalogado: renderizava com o
        nome cru. O mapa de compat em _incidentes preenche a chave `evento`, não a
        busca de descrever(causa)."""
        from apps.scrapers.saude import CATALOGO

        geradas = ("whatsapp_timeout_entrega", "whatsapp_store_recarregado",
                   "whatsapp_preflight_timeout", "whatsapp_frame_recarregado",
                   "whatsapp_confirmacao", "link_afiliado_recusado",
                   "whatsapp_erro_minificado", "publicacao_falhou",
                   "links_sem_sessao", "cupons_vazios", "cupons_campanha_erro")
        faltando = [c for c in geradas if c not in CATALOGO]

        self.assertEqual(faltando, [])


class IncidentesSaudeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("incidente-user", password="test")

    def test_envio_real_posterior_fecha_incidente_do_mesmo_destino(self):
        from apps.scrapers.eventos import log_event
        from apps.scrapers.models import IncidenteSaude

        contexto = {"canal": "whatsapp", "destino": "123@g.us", "causa": "whatsapp_preflight_timeout"}
        log_event("publicacao", "send_failed", "getState timeout", level="warning",
                  usuario=self.user, contexto=contexto)
        incidente = IncidenteSaude.objects.get(usuario=self.user)
        self.assertEqual(incidente.status, "aberto")

        log_event("publicacao", "send_ok", "Oferta publicada com sucesso.",
                  usuario=self.user, contexto={"canal": "whatsapp", "destino": "123@g.us"})
        incidente.refresh_from_db()
        self.assertEqual(incidente.status, "concluido")
        self.assertIn("Envio real", incidente.confirmacao)

    def test_nova_falha_reabre_incidente_confirmado(self):
        from apps.scrapers.eventos import log_event
        from apps.scrapers.models import IncidenteSaude

        contexto = {"canal": "whatsapp", "destino": "123@g.us", "causa": "whatsapp_preflight_timeout"}
        log_event("publicacao", "send_failed", "getState timeout", level="warning", usuario=self.user, contexto=contexto)
        log_event("publicacao", "send_ok", "ok", usuario=self.user,
                  contexto={"canal": "whatsapp", "destino": "123@g.us"})
        log_event("publicacao", "send_failed", "getState timeout", level="warning", usuario=self.user, contexto=contexto)
        incidente = IncidenteSaude.objects.get(usuario=self.user)
        self.assertEqual(incidente.status, "aberto")
        self.assertEqual(incidente.ocorrencias, 2)

    def test_leitura_da_saude_nao_reconta_evento_legado(self):
        """resumo() é só leitura: com auto-refresh, escrever aqui inflaria ocorrências."""
        from apps.scrapers.incidentes_saude import reconciliar_pendentes
        from apps.scrapers.models import IncidenteSaude
        from apps.scrapers.saude import resumo

        EventoOperacional.objects.create(
            pipeline="publicacao", evento="send_failed", level="warning",
            mensagem="getState timeout", usuario=self.user,
            contexto={"canal": "whatsapp", "destino": "123@g.us"},
        )
        reconciliar_pendentes()
        for _ in range(5):                      # simula o polling de 15s
            resumo(usuario=self.user)
        incidente = IncidenteSaude.objects.get(usuario=self.user)
        self.assertEqual(incidente.ocorrencias, 1)

    def test_reconciliar_pendentes_e_idempotente(self):
        """O worker roda em loop: reprojetar o mesmo evento não pode recontar."""
        from apps.scrapers.incidentes_saude import reconciliar_pendentes
        from apps.scrapers.models import IncidenteSaude

        EventoOperacional.objects.create(
            pipeline="publicacao", evento="send_failed", level="warning",
            mensagem="getState timeout", usuario=self.user,
            contexto={"canal": "whatsapp", "destino": "123@g.us"},
        )
        self.assertEqual(reconciliar_pendentes(), 1)
        self.assertEqual(reconciliar_pendentes(), 0)     # já marcado
        self.assertEqual(IncidenteSaude.objects.get(usuario=self.user).ocorrencias, 1)


class ReconexaoBancoScraperTests(TestCase):
    """A raspagem passa minutos no browser antes de salvar; nesse intervalo o socket
    do Postgres pode morrer. O save tem de reconectar e não derrubar o ciclo."""

    def test_upsert_reconecta_e_tenta_de_novo_quando_o_socket_cai(self):
        from django.db import OperationalError
        from apps.scrapers.scraper_mercadolivre import ofertas_scraper

        chamadas = {"n": 0}

        def _falha_na_primeira(**kwargs):
            chamadas["n"] += 1
            if chamadas["n"] == 1:
                raise OperationalError("server closed the connection unexpectedly")
            return (object(), True)

        with patch.object(ofertas_scraper.Produto.objects, "update_or_create",
                          side_effect=_falha_na_primeira) as uoc, \
             patch.object(ofertas_scraper, "_reconectar_db") as reconectar:
            ofertas_scraper._upsert_resiliente(link_produto="x")

        self.assertEqual(uoc.call_count, 2)       # falhou, reconectou, salvou
        reconectar.assert_called_once()

    def test_upsert_nao_engole_erro_persistente(self):
        from django.db import OperationalError
        from apps.scrapers.scraper_mercadolivre import ofertas_scraper

        with patch.object(ofertas_scraper.Produto.objects, "update_or_create",
                          side_effect=OperationalError("caiu de novo")), \
             patch.object(ofertas_scraper, "_reconectar_db"):
            with self.assertRaises(OperationalError):
                ofertas_scraper._upsert_resiliente(link_produto="x")


class InstrumentacaoTests(TestCase):
    """Garante que os pontos que falhavam em silêncio agora deixam rastro."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "instr-user", "instr@x.com", "test")

    def test_email_que_nao_sai_vira_evento(self):
        from apps.accounts.emails import enviar_boas_vindas

        with patch("apps.accounts.emails.EmailMultiAlternatives") as msg:
            msg.return_value.send.side_effect = OSError("SMTP recusou")
            enviado = enviar_boas_vindas(self.user)

        self.assertFalse(enviado)
        evento = EventoOperacional.objects.get(evento="email_falhou")
        self.assertEqual(evento.level, "error")
        self.assertIn("SMTP recusou", evento.erro)

    def _estado(self, conectado, motivo="", detalhe=""):
        from apps.scrapers.conexoes import Estado
        return Estado(conectado, "WhatsApp", "worker", motivo, detalhe, timezone.now())

    def test_queda_de_conexao_vira_evento_mesmo_sem_email(self):
        """O evento não pode depender do e-mail: era exatamente esse o buraco."""
        from apps.scrapers.monitor_conexao import _processar

        perfil = self.user.perfil
        perfil.wa_estado = True
        enviar = Mock(return_value=False)  # SMTP quebrado

        _processar(perfil, "WhatsApp", "wa",
                   self._estado(False, "WhatsApp não está pareado.", "sem_pareamento"),
                   timezone.now(), timedelta(hours=6), enviar)

        evento = EventoOperacional.objects.get(evento="conexao_caiu")
        self.assertEqual(evento.pipeline, "conexao")
        self.assertEqual(evento.level, "error")
        self.assertEqual(evento.usuario, self.user)

    def test_evento_de_queda_carrega_o_motivo(self):
        """"WhatsApp caiu" não é acionável; "não está pareado" vs "serviço fora do
        ar" pedem ações diferentes, e a Saúde só sabe o que o evento contar."""
        from apps.scrapers.monitor_conexao import _processar

        perfil = self.user.perfil
        perfil.wa_estado = True

        _processar(perfil, "WhatsApp", "wa",
                   self._estado(False, "Serviço de WhatsApp indisponível.", "servico_fora"),
                   timezone.now(), timedelta(hours=6), Mock(return_value=False))

        evento = EventoOperacional.objects.get(evento="conexao_caiu")
        self.assertIn("indisponível", evento.mensagem)
        self.assertEqual(evento.contexto["detalhe"], "servico_fora")

    def test_conexao_caida_nao_gera_evento_a_cada_tick(self):
        """Com SMTP quebrado o cooldown precisa segurar mesmo assim.

        O carimbo do alerta só era gravado quando o e-mail ia embora; com SMTP fora,
        ficava None para sempre, o cooldown nunca fechava e cada tick (5min) refazia
        alerta + evento. 288 linhas/dia por usuário caído tornariam a tela inútil.
        """
        from apps.scrapers.monitor_conexao import _processar

        perfil = self.user.perfil
        perfil.wa_estado = True
        enviar = Mock(return_value=False)  # SMTP quebrado, como está em produção hoje
        agora = timezone.now()

        # 12 ticks de 5min = 1 hora caído, dentro do cooldown de 6h.
        for i in range(12):
            _processar(perfil, "WhatsApp", "wa", self._estado(False, "caiu"),
                       agora + timedelta(minutes=5 * i), timedelta(hours=6), enviar)

        self.assertEqual(
            EventoOperacional.objects.filter(evento="conexao_caiu").count(), 1)
        self.assertEqual(enviar.call_count, 1)

    def test_conexao_caida_reaparece_depois_do_cooldown(self):
        """Silenciar não pode virar esquecer: quem segue caído volta a aparecer."""
        from apps.scrapers.monitor_conexao import _processar

        perfil = self.user.perfil
        perfil.wa_estado = True
        agora = timezone.now()
        enviar = Mock(return_value=False)

        _processar(perfil, "WhatsApp", "wa", self._estado(False, "caiu"), agora,
                   timedelta(hours=6), enviar)
        _processar(perfil, "WhatsApp", "wa", self._estado(False, "caiu"),
                   agora + timedelta(hours=7), timedelta(hours=6), enviar)

        eventos = EventoOperacional.objects.filter(evento="conexao_caiu")
        self.assertEqual(eventos.count(), 2)
        self.assertTrue(eventos.order_by("-criado_em").first().contexto["repique"])

    def test_reconexao_vira_evento(self):
        from apps.scrapers.monitor_conexao import _processar

        perfil = self.user.perfil
        perfil.wa_estado = False
        _processar(perfil, "WhatsApp", "wa", self._estado(True), timezone.now(),
                   timedelta(hours=6), Mock(return_value=True))

        self.assertTrue(EventoOperacional.objects.filter(
            evento="conexao_voltou", usuario=self.user).exists())

    @override_settings(PERMITIR_CADASTRO_PUBLICO=True)
    def test_signup_sem_email_de_verificacao_vira_evento(self):
        # O patch é em accounts.emails (não em accounts.views): o import lá é local,
        # resolvido no módulo de origem só na hora da chamada.
        with patch("apps.accounts.emails.enviar_verificacao", return_value=False):
            self.client.post(reverse("signup"), {
                "username": "novo-usuario", "email": "novo@x.com",
                "password1": "senha-forte-123", "password2": "senha-forte-123",
            })

        self.assertTrue(EventoOperacional.objects.filter(
            evento="verificacao_nao_enviada", level="error").exists())

    def test_signup_publico_fechado_por_padrao(self):
        # Produto vendido: cadastro público bloqueado a menos de flag explícita.
        resp = self.client.post(reverse("signup"), {
            "username": "intruso", "email": "intruso@x.com",
            "password1": "senha-forte-123", "password2": "senha-forte-123",
        })
        self.assertRedirects(resp, reverse("login"))
        self.assertFalse(
            get_user_model().objects.filter(username="intruso").exists())


class PurgaEventosTests(TestCase):
    def test_purga_remove_so_o_que_passou_da_janela(self):
        from apps.scrapers.maintenance import purgar_eventos_antigos

        velho = EventoOperacional.objects.create(
            pipeline="sistema", evento="velho", mensagem="x")
        EventoOperacional.objects.filter(pk=velho.pk).update(
            criado_em=timezone.now() - timedelta(days=31))
        EventoOperacional.objects.create(
            pipeline="sistema", evento="novo", mensagem="x")

        apagados = purgar_eventos_antigos(dias=30)

        self.assertEqual(apagados, 1)
        self.assertEqual(
            list(EventoOperacional.objects.values_list("evento", flat=True)), ["novo"])


class EstadoWhatsAppFasesTests(TestCase):
    """estado_whatsapp colapsava toda fase não-conectada em "escaneie o QR".

    Para o MESMO payload do worker, a tela de WhatsApp mostrava progresso azul
    ("Carregando WhatsApp Web…") e a Saúde mostrava erro vermelho — a divergência
    relatada. Fases transitórias agora viram "conectando" (amarelo)."""

    def _estado_para(self, payload):
        from apps.scrapers.conexoes import estado_whatsapp
        with patch("apps.scrapers.whatsapp_client.status", return_value=payload):
            return estado_whatsapp(session="sessao-teste")

    def test_fases_transitorias_viram_conectando_e_nao_erro_de_pareamento(self):
        for fase in ("iniciando", "preparando", "carregando", "autenticado",
                     "sincronizando", "reconectando"):
            estado = self._estado_para({"conectado": False, "fase": fase})
            self.assertFalse(estado.conectado)
            self.assertEqual(estado.detalhe, "conectando", f"fase={fase}")
            self.assertNotIn("QR", estado.motivo)

    def test_capacidade_tem_motivo_proprio(self):
        estado = self._estado_para({"conectado": False, "fase": "capacidade"})
        self.assertEqual(estado.detalhe, "capacidade")
        self.assertIn("limite", estado.motivo.lower())

    def test_recuperacao_pausada_nao_manda_escanear_qr(self):
        # Credencial preservada no worker: reviver resolve, QR novo não é preciso.
        estado = self._estado_para({"conectado": False, "fase": "recuperacao_pausada"})
        self.assertEqual(estado.detalhe, "recuperacao_pausada")
        self.assertNotIn("QR", estado.motivo)

    def test_fases_terminais_seguem_pedindo_pareamento(self):
        for fase in ("inativo", "desconectado", "expirado", "falha_auth", "qr", ""):
            estado = self._estado_para({"conectado": False, "fase": fase})
            self.assertEqual(estado.detalhe, "sem_pareamento", f"fase={fase}")

    def test_conectado_e_erro_seguem_inalterados(self):
        self.assertTrue(self._estado_para({"conectado": True, "fase": "conectado"}).conectado)
        self.assertEqual(
            self._estado_para({"erro": "connection refused", "conectado": False}).detalhe,
            "servico_fora")


class WatchdogFaseTransitoriaTests(TestCase):
    """Deploy do worker derrubava a sessão por segundos e o watchdog mandava
    e-mail "WhatsApp caiu" — o alarme falso relatado."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "watchdog-user", "wd@x.com", "test")

    def test_fase_transitoria_nao_alarma_nem_grava_estado(self):
        from apps.scrapers.conexoes import Estado
        from apps.scrapers.monitor_conexao import _processar

        perfil = self.user.perfil
        perfil.wa_estado = True
        perfil.save(update_fields=["wa_estado"])
        enviar = Mock(return_value=True)

        enviados = _processar(
            perfil, "WhatsApp", "wa",
            Estado(False, "WhatsApp", "worker",
                   "WhatsApp reativando a conexão — aguarde alguns instantes.",
                   "conectando", timezone.now()),
            timezone.now(), timedelta(hours=6), enviar)

        self.assertEqual(enviados, 0)
        enviar.assert_not_called()
        self.assertFalse(EventoOperacional.objects.filter(evento="conexao_caiu").exists())
        # wa_estado intocado: se a reativação falhar, a próxima checagem ainda vê
        # a transição True->False e alerta como primeira vez.
        perfil.refresh_from_db()
        self.assertTrue(perfil.wa_estado)


class WhatsAppPainelSemEfeitoColateralTests(TestCase):
    """O GET da tela de WhatsApp revivia a sessão antes de ler o status — a
    metade "otimista" da divergência com a Saúde. Reviver agora é POST explícito."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("wa-user", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)

    def test_get_da_tela_nao_revive_a_sessao(self):
        with patch("apps.scrapers.whatsapp_client.iniciar_sessao") as iniciar, \
             patch("apps.scrapers.whatsapp_client.status",
                   return_value={"conectado": False, "fase": "inativo"}):
            response = self.client.get(reverse("scraper-whatsapp"))

        self.assertEqual(response.status_code, 200)
        iniciar.assert_not_called()

    def test_post_iniciar_revive_a_sessao_deste_usuario(self):
        with patch("apps.scrapers.whatsapp_client.iniciar_sessao",
                   return_value={"sucesso": True, "fase": "iniciando"}) as iniciar:
            response = self.client.post(reverse("scraper-whatsapp-iniciar"))

        self.assertEqual(response.status_code, 200)
        iniciar.assert_called_once_with(self.user.perfil.sessao_whatsapp())

    def test_iniciar_exige_post(self):
        response = self.client.get(reverse("scraper-whatsapp-iniciar"))
        self.assertEqual(response.status_code, 405)

    def test_reset_suppresses_automatic_revive_until_the_new_qr_arrives(self):
        with patch("apps.scrapers.whatsapp_client.status",
                   return_value={"conectado": False, "fase": "reconectando"}):
            response = self.client.get(reverse("scraper-whatsapp"))

        html = response.content.decode()
        self.assertIn("suprimirReviveAteQr = true", html)
        self.assertIn(
            "suprimirReviveAteQr || reviveTentado || FASES_REVIVIVEIS",
            html,
        )
        # Uma ocorrência é a declaração inicial. O handler do reset não pode
        # mais recolocar reviveTentado=false depois de descartar a sessão.
        self.assertEqual(html.count("reviveTentado = false"), 1)
        self.assertIn("fase === 'reiniciando_qr'", html)
        self.assertIn("fase === 'qr' && s.qr", html)
        self.assertIn("fase === 'falha_reset'", html)
        self.assertIn("QR novo pronto para leitura.", html)
        self.assertIn("Não foi possível gerar o QR. Clique para tentar novamente.", html)


class ChecarAfiliacaoCommandTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "autoteste-afiliacao", password="test")
        self.produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Produto cacheado", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=80,
            link_produto="https://produto.mercadolivre.com.br/MLB-123")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=self.produto, estado="pronto",
            link_afiliado="https://meli.la/sem-atribuicao",
            afiliado_ok=True)

    @patch(
        "apps.scrapers.management.commands.checar_afiliacao.ml_tem_tag",
        return_value=False,
    )
    def test_link_sem_tag_vira_veredito_e_evento_retestavel(self, _tem_tag):
        saida = StringIO()

        call_command(
            "checar_afiliacao", usuario=self.user.get_username(), stdout=saida)

        self.assertIn("não confirmou a atribuição", saida.getvalue())
        evento = EventoOperacional.objects.get(evento="afiliacao_sem_tag")
        self.assertEqual(evento.usuario, self.user)
        self.assertEqual(evento.contexto["causa"], "link_sem_tag")


class CuponsAfiliadosMLTests(SimpleTestCase):
    """Parser da página pública de cupons de afiliados do ML (peça frágil: o HTML
    embute os cupons num array JS; se o formato mudar, isto quebra explicitamente)."""

    HTML = """
    <html><head><script>
      const OUTRA = [1, 2, 3];
      const COUPONS = [
        {"nome":"ATIVO","acao":"Fashion","dia_inicio":"01/01/2099","dia_fim":"31/12/2099",
         "valor_desconto":"20%","min_compra":"49","desconto_max":"60",
         "container_url":"https://lista.mercadolivre.com.br/_Container_x","container_name":"x",
         "is_mar_aberto":false,"days_left":5,"discount_num":20},
        {"nome":"SITEWIDE","acao":"Sellers","dia_inicio":"01/01/2099","dia_fim":"31/12/2099",
         "valor_desconto":"10%","min_compra":"29","desconto_max":"100",
         "container_url":"","container_name":"","is_mar_aberto":true,"days_left":10,"discount_num":10},
        {"nome":"VENCIDO","acao":"Sellers","dia_inicio":"01/01/2020","dia_fim":"02/01/2020",
         "valor_desconto":"30%","min_compra":"0","desconto_max":"0",
         "container_url":"","container_name":"","is_mar_aberto":false,"days_left":0,"discount_num":30}
      ];
      const DEPOIS = [4, 5];
    </script></head><body></body></html>
    """

    def _fake_get(self, *a, **k):
        return Mock(text=self.HTML, raise_for_status=Mock())

    def test_extrai_ativos_ignora_vencidos_e_marca_escopo(self):
        from apps.scrapers.sources.ml_public_coupons import MLPublicCouponsSource
        src = MLPublicCouponsSource()
        with patch("apps.scrapers.sources.ml_public_coupons.requests.get",
                   side_effect=self._fake_get):
            itens = list(src.discover_coupons())

        por_codigo = {it.coupon_code: it for it in itens}
        # VENCIDO (dia_fim em 2020) não entra; os dois ativos entram.
        self.assertEqual(set(por_codigo), {"ATIVO", "SITEWIDE"})
        self.assertTrue(all(it.kind == "coupon" for it in itens))

        site = por_codigo["SITEWIDE"]
        self.assertTrue(site.coupon_rules["is_mar_aberto"])
        self.assertIn("site inteiro", site.title)
        self.assertTrue(site.external_id.endswith(":site"))

        ativo = por_codigo["ATIVO"]
        self.assertEqual(ativo.coupon_rules["valor_desconto"], 20)
        self.assertEqual(ativo.coupon_rules["valor_minimo"], 49)
        self.assertEqual(ativo.coupon_rules["modo_resgate"], "codigo")
        self.assertEqual(ativo.coupon_rules["container_name"], "x")
        self.assertIsNotNone(ativo.valid_until)

    def test_html_sem_array_devolve_vazio(self):
        from apps.scrapers.sources.ml_public_coupons import _extrair_array_js
        self.assertEqual(_extrair_array_js("<html>sem cupons</html>", "COUPONS"), [])


class MelhorCupomNormalizadoTests(TestCase):
    """Gate de confiança do auto-apply de cupom na mensagem (fase 2)."""

    def setUp(self):
        self.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="ml-cupons-afiliados",
            defaults={"marketplace": "mercadolivre", "nome": "Cupons afiliados"})
        self.produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Air fryer", origem="oferta",
            macro_categoria="Casa", categoria="Casa",
            preco_sem_desconto=200, preco_com_cupom=100,
            link_produto="https://example.com/airfryer")

    def _cupom(self, ext, codigo, **regras):
        return CupomNormalizado.objects.create(
            fonte=self.fonte, external_id=ext, marketplace="mercadolivre",
            titulo=codigo, codigo=codigo, estado="ativo",
            link="https://x", regras=regras)

    def test_site_wide_entra_container_sem_confirmacao_nao(self):
        from apps.scrapers.ofertas import _melhor_cupom_normalizado
        self._cupom("a:SITE20", "SITE20", is_mar_aberto=True, discount_num=20, min_compra=0)
        # Desconto maior, mas é de container e não tem match confirmado: NÃO pode entrar.
        self._cupom("a:CONT30", "CONT30", is_mar_aberto=False, discount_num=30, min_compra=0)
        # Site-wide com mínimo acima do preço do item: fora.
        self._cupom("a:MIN99", "MIN99", is_mar_aberto=True, discount_num=99, min_compra=500)

        self.assertEqual(_melhor_cupom_normalizado(self.produto), "SITE20")

    def test_produtocupom_confirmado_libera_cupom_de_container(self):
        from apps.scrapers.models import ProdutoCupom
        from apps.scrapers.ofertas import _melhor_cupom_normalizado
        self._cupom("a:SITE20", "SITE20", is_mar_aberto=True, discount_num=20, min_compra=0)
        conf = self._cupom("a:CONF40", "CONF40", is_mar_aberto=False, discount_num=40, min_compra=0)
        ProdutoCupom.objects.create(produto=self.produto, cupom=conf, status="confirmado")

        # Confirmado e de maior desconto -> vence o site-wide.
        self.assertEqual(_melhor_cupom_normalizado(self.produto), "CONF40")

    def test_sem_cupom_aplicavel_retorna_none(self):
        from apps.scrapers.ofertas import _melhor_cupom_normalizado
        self._cupom("a:CONT30", "CONT30", is_mar_aberto=False, discount_num=30, min_compra=0)
        self.assertIsNone(_melhor_cupom_normalizado(self.produto))


class CasarCuponsContainerTests(TestCase):
    """Casamento cupom-container -> ProdutoCupom confirmado (fase 2), sem Playwright."""

    def setUp(self):
        self.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="ml-cupons-afiliados",
            defaults={"marketplace": "mercadolivre", "nome": "Cupons afiliados"})
        self.no_container = Produto.objects.create(
            marketplace="mercadolivre", nome="Fritadeira", origem="oferta",
            macro_categoria="Casa", categoria="Casa",
            preco_sem_desconto=200, preco_com_cupom=100,
            link_produto="https://produto.mercadolivre.com.br/MLB-1234567-fritadeira")
        self.fora = Produto.objects.create(
            marketplace="mercadolivre", nome="Outro", origem="oferta",
            macro_categoria="Casa", categoria="Casa",
            preco_sem_desconto=200, preco_com_cupom=100,
            link_produto="https://produto.mercadolivre.com.br/MLB-9999999-outro")

    def _cupom(self, ext, codigo, **regras):
        return CupomNormalizado.objects.create(
            fonte=self.fonte, external_id=ext, marketplace="mercadolivre",
            titulo=codigo, codigo=codigo, estado="ativo", link="https://x", regras=regras)

    def test_confirma_produto_presente_no_container_e_ignora_os_de_fora(self):
        from apps.scrapers.models import ProdutoCupom
        from apps.scrapers.scraper_mercadolivre.cupons_container import casar_cupons_container
        cont = self._cupom("a:CONT", "CONT20", is_mar_aberto=False, discount_num=20,
                           container_url="https://lista.mercadolivre.com.br/_Container_x",
                           container_name="x")
        # Site-wide não passa pelo matcher (vale para tudo, não precisa confirmar).
        self._cupom("a:SITE", "SITE10", is_mar_aberto=True, discount_num=10)

        # Coletor fake: o container só contém o item id do produto "no_container".
        total = casar_cupons_container(
            coletor=lambda url, paginas: {"MLB1234567"}, max_paginas=1)

        self.assertEqual(total, 1)
        self.assertTrue(ProdutoCupom.objects.filter(
            produto=self.no_container, cupom=cont, status="confirmado").exists())
        self.assertFalse(ProdutoCupom.objects.filter(produto=self.fora).exists())
        # Nenhum vínculo criado para o cupom site-wide.
        self.assertFalse(ProdutoCupom.objects.filter(cupom__codigo="SITE10").exists())

    def test_sem_cupom_de_container_nao_faz_nada(self):
        from apps.scrapers.scraper_mercadolivre.cupons_container import casar_cupons_container
        self._cupom("a:SITE", "SITE10", is_mar_aberto=True, discount_num=10)
        chamado = {"n": 0}

        def coletor(url, paginas):
            chamado["n"] += 1
            return set()

        self.assertEqual(casar_cupons_container(coletor=coletor), 0)
        self.assertEqual(chamado["n"], 0)  # nem abre container


class MensagemCupomTests(SimpleTestCase):
    def test_formata_esquema_legado_numerico_sem_expor_token(self):
        from apps.scrapers.ofertas import montar_mensagem_cupom
        token = "CATVgkl4DHYJgqaPQXEQ5VMES_mNsb7UfYtN-EXEMPLO=="
        cupom = SimpleNamespace(
            external_id="campanha:123", marketplace="mercadolivre", codigo=token,
            link="https://lista.mercadolivre.com.br/x",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 15.0,
                    "valor_minimo": 79.0},
        )

        mensagem = montar_mensagem_cupom(cupom, link_afiliado="https://meli.la/abc")

        self.assertIn("15% DE DESCONTO", mensagem)
        self.assertIn("acima de R$ 79", mensagem)
        self.assertIn("Ative o cupom no link", mensagem)
        self.assertNotIn(token, mensagem)

    def test_formata_esquema_novo_e_escapa_telegram(self):
        from apps.scrapers.ofertas import montar_mensagem_cupom
        from apps.scrapers.senders.base import TelegramHTMLMarkup
        cupom = SimpleNamespace(
            external_id="afiliados:PROMO:site", marketplace="Loja & Cia",
            codigo="PROMO20", link="https://example.com",
            regras={"valor_desconto": "20%", "min_compra": "R$ 49",
                    "desconto_max": "60", "modo_resgate": "codigo"},
        )

        mensagem = montar_mensagem_cupom(
            cupom, markup=TelegramHTMLMarkup(), link_afiliado="https://example.com?a=1&b=2")

        self.assertIn("Loja &amp; Cia", mensagem)
        self.assertIn("PROMO20", mensagem)
        self.assertIn("limitado a R$ 60", mensagem)
        self.assertIn("a=1&amp;b=2", mensagem)

    def test_json_malformado_nao_levanta(self):
        from apps.scrapers.ofertas import montar_mensagem_cupom
        cupom = SimpleNamespace(external_id="x", marketplace=None, codigo=None,
                                link=None, regras=[1, 2, 3])
        self.assertIn("Ative o cupom", montar_mensagem_cupom(cupom))


class EnvioCupomTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("cupom-send", password="test")
        self.fonte = FonteIngestao.objects.create(
            slug="cupom-send-source", marketplace="mercadolivre", nome="Cupons")
        self.cupom = CupomNormalizado.objects.create(
            fonte=self.fonte, external_id="afiliados:SAVE20:site",
            marketplace="mercadolivre", titulo="20% OFF", codigo="SAVE20",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 20,
                    "modo_resgate": "codigo", "is_mar_aberto": True},
            link="https://www.mercadolivre.com.br/cupons", estado="ativo")

    def _sender(self, resultado):
        from apps.scrapers.senders.base import WhatsAppMarkup
        sender = Mock(markup=WhatsAppMarkup(), prefers_image="b64")
        sender.enviar_oferta.return_value = resultado
        return sender

    @patch("apps.scrapers.ofertas.resolver_link_afiliado_cupom",
           return_value={"sucesso": True, "link": "https://meli.la/afiliado"})
    def test_sucesso_registra_e_bloqueia_mesmo_destino_por_24h(self, _link):
        from apps.scrapers.ofertas import enviar_cupom
        sender = self._sender({"sucesso": True, "via": "whatsapp",
                               "mensagem_id": "m1"})
        with patch("apps.scrapers.senders.registry.get_sender", return_value=sender):
            primeiro = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)
            segundo = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)
            outro = enviar_cupom(self.cupom, "456@g.us", usuario=self.user)

        self.assertTrue(primeiro["sucesso"])
        self.assertTrue(segundo["duplicado"])
        self.assertTrue(outro["sucesso"])
        self.assertEqual(Publicacao.objects.filter(
            origem="cupom", status="enviado", usuario=self.user).count(), 2)

    @patch("apps.scrapers.ofertas.resolver_link_afiliado_cupom",
           return_value={"sucesso": True, "link": "https://meli.la/afiliado"})
    def test_resultado_incerto_e_registrado_e_nao_repetido(self, _link):
        from apps.scrapers.ofertas import enviar_cupom
        sender = self._sender({"sucesso": False, "erro": "confirmação pendente",
                               "classe": "transitorio", "resultado": "incerto",
                               "repetir": False})
        with patch("apps.scrapers.senders.registry.get_sender", return_value=sender):
            primeiro = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)
            segundo = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)

        self.assertEqual(primeiro["resultado"], "incerto")
        self.assertTrue(segundo["duplicado"])
        self.assertEqual(Publicacao.objects.get(usuario=self.user).status, "incerto")

    @patch("apps.scrapers.ofertas.resolver_link_afiliado_cupom",
           return_value={"sucesso": False, "motivo": "sem link"})
    def test_falha_de_afiliacao_fecha_publicacao_e_permite_retentativa(self, _link):
        from apps.scrapers.ofertas import enviar_cupom
        sender = self._sender({"sucesso": True})
        with patch("apps.scrapers.senders.registry.get_sender", return_value=sender):
            primeiro = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)
            segundo = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)
        self.assertFalse(primeiro["sucesso"])
        self.assertFalse(segundo.get("duplicado", False))
        self.assertEqual(Publicacao.objects.filter(status="falhou").count(), 2)


class LinkAfiliadoCupomTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("coupon-link", password="test")
        self.fonte = FonteIngestao.objects.create(
            slug="coupon-link-source", marketplace="mercadolivre", nome="Cupons")
        self.cupom = CupomNormalizado.objects.create(
            fonte=self.fonte, external_id="campanha:123", marketplace="mercadolivre",
            titulo="Ativação", link="https://www.mercadolivre.com.br/cupons/123",
            regras={"modo_resgate": "ativacao"}, estado="ativo")

    @patch("apps.scrapers.scraper_mercadolivre.link.afiliate_link_builder",
           return_value="https://meli.la/cupom-afiliado")
    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_link_direto_verificado_e_cacheado_por_usuario_cupom(self, marketplace, builder):
        from apps.scrapers.ofertas import resolver_link_afiliado_cupom
        marketplace.return_value.verify_affiliate_tag.return_value = True

        primeiro = resolver_link_afiliado_cupom(self.cupom, self.user)
        segundo = resolver_link_afiliado_cupom(self.cupom, self.user)

        self.assertTrue(primeiro["sucesso"])
        self.assertTrue(segundo["cache"])
        builder.assert_called_once()
        marketplace.return_value.verify_affiliate_tag.assert_called_once()

    @patch("apps.scrapers.scraper_mercadolivre.link.afiliate_link_builder",
           return_value="")
    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_fallback_usa_produto_confirmado(self, marketplace, _builder):
        from apps.scrapers.models import ProdutoCupom
        from apps.scrapers.ofertas import resolver_link_afiliado_cupom
        produto = Produto.objects.create(
            nome="Produto compatível", preco_sem_desconto=100, preco_com_cupom=80,
            link_produto="https://produto.mercadolivre.com.br/MLB-123", origem="oferta")
        ProdutoCupom.objects.create(
            produto=produto, cupom=self.cupom, status="confirmado",
            verificado_em=timezone.now())
        marketplace.return_value.build_affiliate_link.return_value = {
            "link_afiliado": "https://meli.la/produto-afiliado", "afiliado_ok": True}

        resultado = resolver_link_afiliado_cupom(self.cupom, self.user)

        self.assertTrue(resultado["sucesso"])
        self.assertEqual(resultado["produto"], produto)


class SenderContractTests(SimpleTestCase):
    def _telegram_user(self):
        return SimpleNamespace(perfil=SimpleNamespace(telegram_bot_token="token-seguro"))

    @patch("apps.scrapers.senders.telegram.requests.post")
    def test_telegram_classifica_429_como_transitorio(self, post):
        from apps.scrapers.senders.telegram import TelegramSender
        post.return_value = Mock(
            status_code=429, json=Mock(return_value={
                "ok": False, "error_code": 429, "description": "Too Many Requests"}))

        resultado = TelegramSender().enviar_oferta(
            "@canal_teste", "mensagem", usuario=self._telegram_user())

        self.assertEqual(resultado["classe"], "transitorio")
        self.assertTrue(resultado["repetir"])
        self.assertEqual(resultado["canal"], "telegram")

    @patch("apps.scrapers.senders.telegram.requests.post")
    def test_telegram_classifica_credencial_como_permanente(self, post):
        from apps.scrapers.senders.telegram import TelegramSender
        post.return_value = Mock(
            status_code=401, json=Mock(return_value={
                "ok": False, "error_code": 401, "description": "Unauthorized"}))

        resultado = TelegramSender().enviar_oferta(
            "@canal_teste", "mensagem", usuario=self._telegram_user())

        self.assertEqual(resultado["classe"], "permanente")
        self.assertFalse(resultado["repetir"])

    def test_canal_desconhecido_e_rejeitado(self):
        from apps.scrapers.senders.registry import get_sender
        with self.assertRaisesMessage(ValueError, "Canal de envio inválido"):
            get_sender("smtp")


class EndpointsEnvioPostTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("endpoint-send", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)

    def test_endpoints_de_envio_rejeitam_get(self):
        for nome in ("scraper-enviar-agora", "scraper-enviar-produto",
                     "scraper-enviar-cupom"):
            with self.subTest(nome=nome):
                self.assertEqual(self.client.get(reverse(nome)).status_code, 405)

    def test_post_sem_csrf_e_rejeitado(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(reverse("scraper-enviar-cupom"), {
            "cupom": 1, "grupo": "123@g.us", "canal": "whatsapp",
        })
        self.assertEqual(response.status_code, 403)

    def test_id_com_sql_injection_nao_e_interpretado(self):
        response = self.client.post(reverse("scraper-enviar-cupom"), {
            "cupom": "1 OR 1=1; DROP TABLE scrapers_cupomnormalizado",
            "grupo": "123@g.us", "canal": "whatsapp",
        })
        corpo = b"".join(response.streaming_content).decode()
        self.assertIn("Cupom não encontrado", corpo)
        self.assertNotIn("DROP TABLE", corpo)

    def test_html_escapa_campos_e_oculta_token_tecnico(self):
        fonte = FonteIngestao.objects.create(
            slug="xss-coupon-source", marketplace="mercadolivre", nome="Fonte")
        token = "CATVgkl4DHYJgqaPQXEQ5VMES_mNsb7UfYtN-SEGREDO=="
        CupomNormalizado.objects.create(
            fonte=fonte, external_id="campanha:xss", marketplace="mercadolivre",
            titulo='<script>alert("xss")</script>', codigo=token,
            regras={"modo_resgate": "ativacao"}, estado="ativo")

        response = self.client.get(reverse("scraper-top"), {
            "tipo": "cupom", "afiliado": "todos"})
        corpo = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("&lt;script&gt;", corpo)
        self.assertNotIn('<script>alert("xss")</script>', corpo)
        self.assertNotIn(token, corpo)
        self.assertIn("Ativar no link", corpo)
