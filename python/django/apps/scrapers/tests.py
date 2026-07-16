import os
import tempfile
import uuid
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
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.scrapers import ofertas, whatsapp_client
from apps.scrapers.afiliado import tag_ml
from apps.scrapers.maintenance import reconciliar_publicacoes_orfas
from apps.scrapers.management.commands.automacao import _rodar_links
from apps.scrapers.marketplaces.registry import get_marketplace
from apps.scrapers.monitor_conexao import wa_conectado
from apps.scrapers.models import (
    CliquePublicacao, ConfiguracaoEnvio, Cupom, FonteIngestao, HistoricoEnvio,
    LinkAfiliadoUsuario, Produto, EventoOperacional, Publicacao,
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
            "grupo@g.us", "mensagem", usuario=self.user)

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
        self.assertEqual(post.call_args.kwargs["timeout"], 75)

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
        Produto.objects.create(
            marketplace="mercadolivre",
            nome="Fone Bluetooth",
            categoria="Áudio",
            macro_categoria="Eletrônicos",
            preco_sem_desconto=100,
            preco_com_cupom=50,
            link_produto="https://example.com/fone",
            origem="oferta",
        )
        Produto.objects.create(
            marketplace="amazon",
            owner=self.user,
            nome="Cafeteira",
            categoria="Cozinha",
            macro_categoria="Casa",
            preco_sem_desconto=100,
            preco_com_cupom=90,
            link_produto="https://example.com/cafeteira",
            origem="oferta",
        )

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
        product = Produto.objects.create(
            marketplace="mercadolivre",
            nome="Panela com cupom vencido",
            categoria="Cozinha",
            macro_categoria="Casa",
            campanha_id="expired-coupon",
            preco_sem_desconto=200,
            preco_com_cupom=120,
            link_produto="https://example.com/panela",
            origem="oferta",
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
        stale = Produto.objects.create(
            marketplace="mercadolivre",
            nome="Oferta velha",
            categoria="Cozinha",
            macro_categoria="Casa",
            preco_sem_desconto=100,
            preco_com_cupom=50,
            link_produto="https://example.com/stale",
            origem="oferta",
            estado="stale",
        )

        response = self.client.get(self.url, {"q": "Oferta velha"})

        self.assertNotIn(stale.id, [p.id for p in response.context["produtos"]])

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
    def test_dashboard_sync_now_uses_automatic_sync(self, sync_marketplace):
        sync_marketplace.return_value = RelatorioSync.objects.create(
            usuario=self.user, marketplace="mercadolivre", status="ok",
            registros_criados=1, registros_atualizados=0,
        )

        response = self.client.post(reverse("scraper-sincronizar-receitas"), {
            "marketplace": "mercadolivre",
        })

        self.assertRedirects(response, reverse("home"))
        sync_marketplace.assert_called_once_with(self.user, "mercadolivre")

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
        from apps.scrapers.ofertas import montar_mensagem
        config = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="group@g.us", nome_marca="Tech do Dia",
            chamada_acao="Ver a oferta",
        )
        message = montar_mensagem(
            self.product, "https://example.com/a", None,
            usuario=self.user, configuracao=config,
        )
        self.assertIn("Tech do Dia", message)
        self.assertIn("Ver a oferta", message)

    @patch("apps.scrapers.ofertas.os.getenv", return_value="")
    @override_settings(DEBUG=True)
    def test_local_delivery_uses_affiliate_link_directly_without_public_url(self, _env):
        """O redirecionador de produção não conhece a publicação do SQLite local."""
        from apps.scrapers.ofertas import _link_publicado

        publication = Mock(id_publico=uuid.uuid4())
        affiliate = "https://meli.la/link-afiliado"
        self.assertEqual(_link_publicado(publication, affiliate), affiliate)

    @override_settings(DEBUG=False, PUBLIC_BASE_URL="https://spreading.example")
    def test_production_delivery_uses_signed_tracking_redirect(self):
        from apps.scrapers.ofertas import _link_publicado

        publication = Mock(id_publico=uuid.uuid4())
        link = _link_publicado(publication, "https://meli.la/link-afiliado")
        self.assertTrue(link.startswith("https://spreading.example/scrapers/r/"))


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
        consultas_de_link = [
            q for q in ctx.captured_queries
            if "linkafiliadousuario" in q["sql"].lower()
        ]
        self.assertEqual(len(consultas_de_link), 1, consultas_de_link)

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
        self.assertEqual(res, {"gerados": 1, "falhas": 0})

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
        self._produto()

        self.assertEqual(_rodar_links(lote=40), {"gerados": 0, "falhas": 0})
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

    @patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=True)
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.prefetch_links",
           side_effect=RuntimeError("sessão expirada"))
    def test_falha_de_um_usuario_nao_derruba_o_ciclo(self, _prefetch, _conectado):
        # A sessão ML é de cada um: a do vizinho vencer não pode me impedir de gerar.
        self._produto()

        self.assertEqual(_rodar_links(lote=40), {"gerados": 0, "falhas": 0})


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
        return EventoOperacional.objects.create(
            pipeline=pipeline, evento=evento, level=level,
            mensagem=kw.pop("mensagem", "falhou"), usuario=kw.pop("usuario", self.user),
            contexto=kw.pop("contexto", {}),
        )

    def test_agrupa_ocorrencias_repetidas_num_problema_so(self):
        from apps.scrapers.saude import resumo

        for _ in range(4):
            self._evento("send_failed", level="warning")

        r = resumo(horas=24)
        self.assertEqual(len(r["problemas"]), 1)
        self.assertEqual(r["problemas"][0]["n"], 4)
        self.assertEqual(r["problemas"][0]["usuarios"], 1)

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

    def test_ignora_evento_fora_da_janela(self):
        from apps.scrapers.saude import resumo

        antigo = self._evento("config_pausada")
        EventoOperacional.objects.filter(pk=antigo.pk).update(
            criado_em=timezone.now() - timedelta(hours=48))

        self.assertEqual(resumo(horas=24)["problemas"], [])
        self.assertEqual(len(resumo(horas=168)["problemas"]), 1)

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

    def test_queda_de_conexao_vira_evento_mesmo_sem_email(self):
        """O evento não pode depender do e-mail: era exatamente esse o buraco."""
        from apps.scrapers.monitor_conexao import _processar

        perfil = self.user.perfil
        perfil.wa_estado = True
        enviar = Mock(return_value=False)  # SMTP quebrado

        _processar(perfil, "WhatsApp", "wa", False, timezone.now(),
                   timedelta(hours=6), enviar)

        evento = EventoOperacional.objects.get(evento="conexao_caiu")
        self.assertEqual(evento.pipeline, "conexao")
        self.assertEqual(evento.level, "error")
        self.assertEqual(evento.usuario, self.user)

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
            _processar(perfil, "WhatsApp", "wa", False, agora + timedelta(minutes=5 * i),
                       timedelta(hours=6), enviar)

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

        _processar(perfil, "WhatsApp", "wa", False, agora, timedelta(hours=6), enviar)
        _processar(perfil, "WhatsApp", "wa", False, agora + timedelta(hours=7),
                   timedelta(hours=6), enviar)

        eventos = EventoOperacional.objects.filter(evento="conexao_caiu")
        self.assertEqual(eventos.count(), 2)
        self.assertTrue(eventos.order_by("-criado_em").first().contexto["repique"])

    def test_reconexao_vira_evento(self):
        from apps.scrapers.monitor_conexao import _processar

        perfil = self.user.perfil
        perfil.wa_estado = False
        _processar(perfil, "WhatsApp", "wa", True, timezone.now(),
                   timedelta(hours=6), Mock(return_value=True))

        self.assertTrue(EventoOperacional.objects.filter(
            evento="conexao_voltou", usuario=self.user).exists())

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
