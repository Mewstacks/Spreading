import asyncio
import hashlib
import hmac
import itertools
import json
import os
import re
import tempfile
import uuid
from types import SimpleNamespace
from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.tenant import organization_context
from apps.scrapers import hooks, ofertas, whatsapp_client
from apps.scrapers.afiliado import tag_ml
from apps.scrapers.carga import BrowserResourceUnavailable
from apps.scrapers.maintenance import reconciliar_publicacoes_orfas
from apps.scrapers.management.commands.automacao import _rodar_links
from apps.scrapers.marketplaces.registry import get_marketplace
from apps.scrapers.monitor_conexao import wa_conectado
from apps.scrapers.models import (
    CliquePublicacao, ConfiguracaoEnvio, Cupom, CupomDisponibilidade,
    CupomNormalizado, FonteIngestao,
    HistoricoEnvio, LinkAfiliadoUsuario, Produto, EventoOperacional, Publicacao,
    ReceitaAfiliado, RelatorioSync,
)
from apps.scrapers.precos import registrar as registrar_preco
from apps.scrapers.scraper_amazon import link as amazon_link
from apps.scrapers.scraper_amazon import ofertas_scraper as amazon_ofertas
from apps.scrapers.scraper_mercadolivre.scraper import _sincronizar_produtos_no_banco
from apps.scrapers.scraper_mercadolivre import link as ml_link

_TEST_WA_HEADERS = {
    "Authorization": "Bearer test-capability",
    "Content-Type": "application/json",
}


def _mock_wa_capability(testcase):
    patcher = patch(
        "apps.scrapers.whatsapp_client._headers",
        return_value=_TEST_WA_HEADERS,
    )
    patcher.start()
    testcase.addCleanup(patcher.stop)


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

    def test_usuario_comum_nao_liga_worker(self):
        for tipo in ("envio", "scrape"):
            with self.subTest(tipo=tipo):
                response = self.client.post(
                    reverse("scraper-automacao"), {"tipo": tipo, "acao": "start"})
                self.assertEqual(response.status_code, 403)

    @patch("apps.scrapers.automacao_state.spawn_worker")
    @patch("apps.scrapers.automacao_state.is_running", return_value=True)
    def test_permissao_delegada_liga_so_o_envio(self, _running, _spawn):
        # Delegação estreita: o botão do envio abre, a raspagem continua só p/ staff.
        self.user.perfil.pode_ligar_envio = True
        self.user.perfil.save(update_fields=["pode_ligar_envio"])

        ok = self.client.post(reverse("scraper-automacao"), {"tipo": "envio", "acao": "start"})
        self.assertEqual(ok.status_code, 200)

        negado = self.client.post(reverse("scraper-automacao"), {"tipo": "scrape", "acao": "start"})
        self.assertEqual(negado.status_code, 403)

    @patch("apps.scrapers.maintenance.purgar_eventos_antigos", return_value=0)
    @patch("apps.scrapers.maintenance.reconciliar_publicacoes_orfas", return_value=0)
    @patch("apps.scrapers.ofertas.processar_configs_de_envio", return_value=[])
    @patch("apps.scrapers.management.commands.automacao._renovar_conexoes_db")
    @patch("apps.scrapers.management.commands.automacao.st.write_state")
    @patch("apps.scrapers.management.commands.automacao.st.is_enabled", return_value=True)
    def test_worker_envio_renova_heartbeat_durante_espera(
        self, _enabled, write_state, _renovar, _processar, _orfas, _purgar,
    ):
        """O tick é 5 min e o heartbeat vence em 90s; a espera precisa escrever.

        Três sleeps sem a correção mantinham uma única escrita `aguardando`
        (fim do ciclo). Com ela, cada passagem de 15s renova o arquivo de estado.
        """
        from apps.scrapers.management.commands.automacao import Command

        sleeps = {"n": 0}

        def parar_depois_de_tres(_segundos):
            sleeps["n"] += 1
            if sleeps["n"] == 3:
                raise RuntimeError("fim do loop de teste")

        with patch(
            "apps.scrapers.management.commands.automacao.time.sleep",
            side_effect=parar_depois_de_tres,
        ):
            with self.assertRaisesRegex(RuntimeError, "fim do loop de teste"):
                Command()._loop_envio({"tick": 5})

        # O heartbeat atualiza somente o timestamp: repetir ``fase`` apagaria o
        # erro/proximo_ciclo persistido pelo tick anterior.
        pulsos = [
            chamada for chamada in write_state.call_args_list
            if chamada.args == ("envio",) and not chamada.kwargs
        ]
        self.assertGreaterEqual(len(pulsos), 3)


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

    def test_ml_link_uses_only_the_users_encrypted_session(self):
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
            self.assertIs(builder.call_args.kwargs["usuario"], self.user)
            self.assertNotIn("auth_path", builder.call_args.kwargs)
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

    def test_job_sem_usuario_recusa_auth_global_legado(self):
        from apps.scrapers.session_paths import ml_auth_path

        with tempfile.TemporaryDirectory() as d, override_settings(ML_AUTH_DIR=d):
            self._tocar(os.path.join(d, "auth.json"))
            self._tocar(os.path.join(d, "auth_7.json"))
            self.assertEqual(ml_auth_path(), "")

    def test_job_sem_usuario_nunca_escolhe_sessao_mais_recente(self):
        """O que conserta cron/cupons: sem auth.json, usar a sessão real que existe."""
        from apps.scrapers.session_paths import ml_auth_path

        with tempfile.TemporaryDirectory() as d, override_settings(ML_AUTH_DIR=d):
            self._tocar(os.path.join(d, "auth_1.json"), quando=1_000_000)
            self._tocar(os.path.join(d, "auth_2.json"), quando=2_000_000)
            self.assertEqual(ml_auth_path(), "")

    def test_sem_usuario_devolve_vazio(self):
        """Não estoura aqui: quem chama reporta 'reconecte' com a mensagem certa."""
        from apps.scrapers.session_paths import ml_auth_path

        with tempfile.TemporaryDirectory() as d, override_settings(ML_AUTH_DIR=d):
            caminho = ml_auth_path()
            self.assertEqual(caminho, "")

    def test_arquivo_alheio_no_diretorio_nao_vira_sessao(self):
        from apps.scrapers.session_paths import ml_auth_path

        with tempfile.TemporaryDirectory() as d, override_settings(ML_AUTH_DIR=d):
            self._tocar(os.path.join(d, "auth_1.json.bak"))
            self._tocar(os.path.join(d, "outra_coisa.json"))
            self.assertEqual(ml_auth_path(), "")


class SondaSessaoMLTests(SimpleTestCase):
    """A sonda pergunta ao ML se a sessão salva ainda vale.

    Ela NÃO decide desconectar ninguém: devolve "suspeito", e a acumulação de
    suspeitas em accounts.ml_sessions.registrar_veredito é que muda o estado. Do
    IP de datacenter da Fly, o gateway anti-bot do ML responde 302→login e 403 a
    requisições perfeitamente autenticadas — um veredito isolado não distingue
    challenge de logout.
    """

    STATE = {"cookies": [{"name": "ssid", "value": "x", "domain": ".mercadolivre.com.br",
                          "path": "/"}], "origins": []}

    @staticmethod
    def _resposta(status, location=None):
        return Mock(status_code=status,
                    headers={"Location": location} if location else {})

    def _sondar(self, **kwargs):
        from apps.scrapers.conexoes import sondar_sessao_ml

        with patch("requests.Session.get", **kwargs):
            return sondar_sessao_ml(self.STATE)

    def test_200_e_sessao_viva(self):
        self.assertEqual(self._sondar(return_value=self._resposta(200)),
                         ("conectado", ""))

    def test_redirect_para_login_e_suspeito_nao_expirado(self):
        """Redirect p/ login é suspeita, não sentença: o anti-bot faz isso com
        cookie válido, e tratar como expirado desconectava quem tinha acabado de
        conectar."""
        veredito, _ = self._sondar(return_value=self._resposta(
            302, "https://www.mercadolivre.com.br/jms/mlb/lgz/login"))
        self.assertEqual(veredito, "suspeito")

    def test_403_e_inconclusivo(self):
        """403 é o gateway anti-bot barrando o IP antes de olhar o cookie."""
        veredito, _ = self._sondar(return_value=self._resposta(403))
        self.assertEqual(veredito, "inconclusivo")

    def test_timeout_e_inconclusivo_nao_expirado(self):
        """Oscilação de rede não é logout — a lição de auxiliar.py."""
        veredito, _ = self._sondar(side_effect=requests.Timeout("estourou"))
        self.assertEqual(veredito, "inconclusivo")

    def test_erro_do_ml_e_inconclusivo(self):
        """5xx é problema do ML, não da sessão: não pode desconectar o usuário."""
        veredito, _ = self._sondar(return_value=self._resposta(503))
        self.assertEqual(veredito, "inconclusivo")

    def test_cookies_homonimos_de_dominios_diferentes_convivem(self):
        """O storage_state do ML tem `ssid` em vários domínios.

        O jar antigo era um dict {name: value}: o último do arquivo vencia e ia
        para o host errado, então a sonda tomava 302→login sobre uma sessão viva —
        e o veredito apagava a sessão.
        """
        from apps.scrapers.ml_auth import http_session

        sessao = http_session({"cookies": [
            {"name": "ssid", "value": "correto", "domain": ".mercadolivre.com.br", "path": "/"},
            {"name": "ssid", "value": "outro", "domain": ".mercadopago.com.br", "path": "/"},
        ]})
        valores = {c.domain: c.value for c in sessao.cookies}
        self.assertEqual(valores[".mercadolivre.com.br"], "correto")
        self.assertEqual(valores[".mercadopago.com.br"], "outro")


class EsperaDeReconexaoDoWhatsAppTests(SimpleTestCase):
    """O gate de canal ESPERA a reconexão em vez de recusar o envio na hora.

    Era a queixa: "estou enviando promoções e do nada aparece que o canal de envio
    não é mais válido". A reconexão automática já existia no worker; o que faltava
    era o envio dar a ela alguns segundos. Uma queda de rede de 5s virava erro na
    tela, embora a sessão voltasse sozinha em seguida.
    """

    def setUp(self):
        cache.clear()

    @staticmethod
    def _estado(conectado, detalhe="", motivo=""):
        from apps.scrapers.conexoes import Estado

        return Estado(conectado, "WhatsApp", "worker", motivo, detalhe, None)

    @staticmethod
    def _relogio():
        """Relógio falso: cada leitura avança um intervalo de espera.

        Sem isto o teste dependeria do relógio real — com `time.sleep` mockado, a
        espera de 20s viraria milhares de iterações instantâneas (ou 20s de teste).
        """
        from apps.scrapers import ofertas

        contador = itertools.count(0, ofertas._WA_ESPERA_INTERVALO_S)
        return lambda: next(contador)

    def _canal(self, estados, *, insistir=False):
        """Roda o gate com uma sequência de estados; devolve (erro, chamadas)."""
        from apps.scrapers import ofertas

        sequencia = (itertools.chain(estados, itertools.repeat(estados[-1]))
                     if insistir else list(estados))
        with patch("apps.scrapers.ofertas.wa_session_de", return_value="s1"), \
             patch("apps.scrapers.conexoes.estado_whatsapp",
                   side_effect=sequencia) as le, \
             patch("apps.scrapers.whatsapp_client.invalidar_status"), \
             patch("apps.scrapers.whatsapp_client.status",
                   return_value={"conectado": False, "fase": "reconectando"}), \
             patch("time.sleep"), \
             patch("time.monotonic", side_effect=self._relogio()):
            return ofertas._canal_pronto_ou_erro("whatsapp", object()), le.call_count

    def test_conectado_de_primeira_nao_espera(self):
        erro, chamadas = self._canal([self._estado(True)])
        self.assertIsNone(erro)
        self.assertEqual(chamadas, 1)

    def test_reconectando_nao_bloqueia_a_thread_web(self):
        """O request devolve transitório; o worker assíncrono fará a retomada."""
        erro, _ = self._canal([
            self._estado(False, "conectando", "WhatsApp reativando a conexão."),
            self._estado(False, "conectando"),
            self._estado(True),
        ])
        self.assertEqual(erro["classe"], "transitorio")
        self.assertEqual(erro["etapa"], "transport_queued")

    def test_reconectando_que_nao_volta_ainda_e_transitorio(self):
        """Não conta falha da configuração: `pausar_apos_falhas` não pode desligar
        a automação por uma queda de infraestrutura."""
        parado = self._estado(False, "conectando", "WhatsApp reativando a conexão.")
        erro, _ = self._canal([parado], insistir=True)
        self.assertIsNotNone(erro)
        self.assertEqual(erro["classe"], "transitorio")
        # E não pede login: não há QR a ler, é só esperar.
        self.assertNotIn("precisa_login_wa", erro)

    def test_sem_pareamento_nao_espera_nada(self):
        """Estado terminal: esperar só atrasaria a mensagem que pede ação."""
        sem_par = self._estado(False, "sem_pareamento",
                               "WhatsApp desconectado. Reconecte sua conta.")
        erro, chamadas = self._canal([sem_par, sem_par])
        self.assertIsNotNone(erro)
        self.assertTrue(erro["precisa_login_wa"])
        # Uma leitura no gate; nenhuma volta de espera.
        self.assertEqual(chamadas, 1)

    def test_sessao_inativa_religa_sem_bloquear_o_request(self):
        from apps.scrapers import ofertas

        with patch("apps.scrapers.ofertas.wa_session_de", return_value="s1"), \
             patch("apps.scrapers.conexoes.estado_whatsapp", side_effect=[
                 self._estado(False, "sem_pareamento", "desconectado"),
                 self._estado(False, "conectando"),
                 self._estado(True),
             ]), \
             patch("apps.scrapers.whatsapp_client.invalidar_status"), \
             patch("apps.scrapers.whatsapp_client.status",
                   return_value={"conectado": False, "fase": "inativo"}), \
             patch("apps.scrapers.whatsapp_client.iniciar_sessao") as religar, \
             patch("time.sleep"):
            erro = ofertas._canal_pronto_ou_erro("whatsapp", object())

        religar.assert_called_once()
        self.assertEqual(erro["classe"], "transitorio")
        self.assertEqual(erro["etapa"], "transport_queued")

    def test_canal_que_nao_e_whatsapp_passa_direto(self):
        from apps.scrapers import ofertas

        self.assertIsNone(ofertas._canal_pronto_ou_erro("telegram", object()))


class MensagemDeCanalReconectandoTests(SimpleTestCase):
    """A tradução da falha de transporte não pode pedir o que não existe.

    "As credenciais do canal precisam ser reconectadas" mandava o usuário procurar
    uma reconexão manual que o worker já estava fazendo sozinho.
    """

    def test_reconexao_em_curso_diz_que_e_automatica(self):
        from apps.scrapers.ofertas import _motivo_publico_transporte

        texto = _motivo_publico_transporte({
            "classe": "transitorio",
            "erro": "WhatsApp reconectando — o envio será retomado.",
        })
        self.assertIn("volta", texto.lower())
        self.assertNotIn("credenciais", texto.lower())

    def test_outras_falhas_transitorias_mantem_o_texto_antigo(self):
        from apps.scrapers.ofertas import _motivo_publico_transporte

        texto = _motivo_publico_transporte({"classe": "transitorio", "erro": "429"})
        self.assertIn("temporariamente indisponível", texto)

    def test_credencial_realmente_permanente_continua_pedindo_reconexao(self):
        from apps.scrapers.ofertas import _motivo_publico_transporte

        texto = _motivo_publico_transporte({
            "classe": "permanente", "erro": "token invalido"})
        self.assertIn("reconectadas", texto)


class SondaLinkBuilderTests(SimpleTestCase):
    """A sonda do PORTAL DE AFILIADOS, que não existia.

    O portal /afiliados/linkbuilder tem SSO próprio (jms/msl): um cookie que
    mercadolivre.com.br ainda aceita pode ser recusado lá. Enquanto só
    `sondar_sessao_ml` existia, todas as telas diziam "conectado" e a geração de
    links respondia "sessão expirada" — o relato que originou este trabalho.

    Vocabulário e política são os mesmos da sonda do site, de propósito: só a
    repetição de "suspeito" pede reconexão, e 403 nunca é logout.
    """

    STATE = {"cookies": [{"name": "ssid", "value": "x", "domain": ".mercadolivre.com.br",
                          "path": "/"}], "origins": []}

    @staticmethod
    def _resposta(status, location=None):
        return Mock(status_code=status,
                    headers={"Location": location} if location else {})

    def _sondar(self, **kwargs):
        from apps.scrapers.conexoes import sondar_portal_afiliados_ml

        with patch("requests.Session.get", **kwargs):
            return sondar_portal_afiliados_ml(self.STATE)

    def test_sonda_bate_no_portal_e_nao_no_site(self):
        """Sondar myaccount não responde a pergunta: é o portal que recusa."""
        from apps.scrapers.conexoes import sondar_portal_afiliados_ml

        with patch("requests.Session.get", return_value=self._resposta(200)) as get:
            sondar_portal_afiliados_ml(self.STATE)
        (url,), _ = get.call_args
        self.assertIn("/afiliados/linkbuilder", url)

    def test_200_e_portal_aceitando(self):
        self.assertEqual(self._sondar(return_value=self._resposta(200)),
                         ("conectado", ""))

    def test_redirect_para_login_e_suspeito(self):
        veredito, motivo = self._sondar(return_value=self._resposta(
            302, "https://www.mercadolivre.com.br/jms/mlb/lgz/login"))
        self.assertEqual(veredito, "suspeito")
        self.assertIn("portal de afiliados", motivo)

    def test_intersticial_e_inconclusivo(self):
        """Verificação de segurança interposta: a conta segue conectada. Contar
        isto como suspeita desconectaria por ruído do anti-bot."""
        veredito, _ = self._sondar(return_value=self._resposta(
            302, "https://www.mercadolivre.com.br/gz/account-verification"))
        self.assertEqual(veredito, "inconclusivo")

    def test_403_e_inconclusivo(self):
        veredito, _ = self._sondar(return_value=self._resposta(403))
        self.assertEqual(veredito, "inconclusivo")

    def test_timeout_e_5xx_sao_inconclusivos(self):
        self.assertEqual(self._sondar(side_effect=requests.Timeout("x"))[0],
                         "inconclusivo")
        self.assertEqual(self._sondar(return_value=self._resposta(502))[0],
                         "inconclusivo")

    def test_sem_cookies_e_suspeito(self):
        from apps.scrapers.conexoes import sondar_portal_afiliados_ml

        self.assertEqual(
            sondar_portal_afiliados_ml({"cookies": [], "origins": []})[0], "suspeito")


class ProntidaoRealDoLinkBuilderTests(TestCase):
    def setUp(self):
        from apps.accounts.ml_sessions import save_storage_state

        cache.clear()
        self.user = get_user_model().objects.create_user("lb-real", password="test")
        self.org = self.user.personal_organization
        save_storage_state(
            self.user,
            {"cookies": [{"name": "ssid", "value": "x"}], "origins": []},
        )

    def _estado(self):
        from apps.scrapers.conexoes import estado_ml_linkbuilder

        cache.clear()
        return estado_ml_linkbuilder(self.user, usar_cache=False)

    def test_http_200_nunca_promove_unknown_para_ready(self):
        with patch(
            "apps.scrapers.conexoes.sondar_portal_afiliados_ml",
            return_value=("conectado", ""),
        ) as sonda:
            estado = self._estado()

        self.assertFalse(estado.conectado)
        self.assertEqual(estado.detalhe, "unknown")
        sonda.assert_not_called()

    def test_transicoes_reais_e_validade_de_quinze_minutos(self):
        from apps.accounts.ml_sessions import registrar_prontidao_linkbuilder
        from apps.accounts.models import MercadoLivreSession

        registrar_prontidao_linkbuilder(self.org, "ready", "controles confirmados")
        self.assertTrue(self._estado().conectado)
        self.assertEqual(self._estado().detalhe, "ready")

        MercadoLivreSession.objects.filter(organization=self.org).update(
            lb_readiness_checked_at=timezone.now() - timedelta(minutes=16),
        )
        self.assertEqual(self._estado().detalhe, "stale")

        registrar_prontidao_linkbuilder(
            self.org, "login_required", "portal pediu login",
        )
        self.assertEqual(self._estado().detalhe, "login_required")

        registrar_prontidao_linkbuilder(
            self.org, "temporarily_unavailable", "anti-bot",
        )
        self.assertEqual(self._estado().detalhe, "temporarily_unavailable")


class ControlesReaisDoLinkBuilderTests(SimpleTestCase):
    """Protege as duas versões de DOM vistas no portal do Mercado Livre."""

    class Locator:
        def __init__(self, *, visivel=False, habilitado=True, ao_preencher=None):
            self.visivel = visivel
            self.habilitado = habilitado
            self.ao_preencher = ao_preencher
            self.first = self
            self.preenchido = None
            self.clicado = False

        def is_visible(self, timeout=None):
            return self.visivel

        def is_enabled(self, timeout=None):
            return self.habilitado

        def fill(self, valor):
            self.preenchido = valor
            if self.ao_preencher:
                self.ao_preencher(valor)

        def input_value(self, timeout=None):
            return self.preenchido or ""

        def click(self):
            self.clicado = True

        def filter(self, **kwargs):
            return self

    class Page:
        def __init__(self, *, textarea=False, legado=False):
            self.botao = ControlesReaisDoLinkBuilderTests.Locator(visivel=True)

            def habilitar_com_url(valor):
                self.botao.habilitado = bool(valor)

            self.campo_atual = ControlesReaisDoLinkBuilderTests.Locator(
                visivel=textarea, ao_preencher=habilitar_com_url,
            )
            self.campo_legado = ControlesReaisDoLinkBuilderTests.Locator(
                visivel=legado, ao_preencher=habilitar_com_url,
            )

        def locator(self, seletor):
            if seletor.startswith("textarea"):
                return self.campo_atual
            if seletor == "button":
                return self.botao
            return ControlesReaisDoLinkBuilderTests.Locator()

        def get_by_role(self, role, name=None, exact=None):
            if role == "textbox":
                return self.campo_legado
            if role == "button" and name == "Gerar":
                return self.botao
            return ControlesReaisDoLinkBuilderTests.Locator()

    def test_dom_atual_com_textarea_sem_nome_acessivel_fica_ready(self):
        from apps.scrapers.scraper_mercadolivre.link import _linkbuilder_pronto

        self.assertTrue(_linkbuilder_pronto(self.Page(textarea=True)))

    def test_dom_legado_com_nome_acessivel_continua_ready(self):
        from apps.scrapers.scraper_mercadolivre.link import _linkbuilder_pronto

        self.assertTrue(_linkbuilder_pronto(self.Page(legado=True)))

    def test_botao_desabilitado_em_campo_vazio_e_exercitado_sem_submeter(self):
        from apps.scrapers.scraper_mercadolivre.link import _linkbuilder_pronto

        page = self.Page(textarea=True)
        page.botao.habilitado = False

        self.assertTrue(_linkbuilder_pronto(page))
        self.assertEqual(page.campo_atual.preenchido, "")
        self.assertFalse(page.botao.clicado)

    def test_geracao_preenche_o_textarea_atual_e_clica_em_gerar(self):
        from apps.scrapers.scraper_mercadolivre import link as ml

        page = self.Page(textarea=True)
        with patch.object(ml, "_limpar_resultado"), \
             patch.object(ml, "_esperar_resultado", return_value="https://meli.la/x"), \
             patch.object(ml, "_validar_resultado_link", return_value="https://meli.la/x"):
            resultado = ml._afiliar_url_na_pagina(
                page, "https://produto.mercadolivre.com.br/MLB-123",
            )

        self.assertEqual(resultado, "https://meli.la/x")
        self.assertEqual(
            page.campo_atual.preenchido,
            "https://produto.mercadolivre.com.br/MLB-123",
        )
        self.assertTrue(page.botao.clicado)


class VereditoLinkBuilderForaDoLoopORMTests(TransactionTestCase):
    """Regressão do SynchronousOnlyOperation visto em produção."""

    def test_veredito_real_e_gravado_com_event_loop_ativo(self):
        from apps.accounts.models import MercadoLivreSession
        from apps.accounts.ml_sessions import save_storage_state
        from apps.accounts.tenant import tenant_suspenso
        from apps.scrapers.scraper_mercadolivre.link import _registrar_veredito_lb

        user = get_user_model().objects.create_user("lb-async-bridge", password="test")
        organization = user.personal_organization
        save_storage_state(
            user,
            {"cookies": [{"name": "ssid", "value": "x"}], "origins": []},
        )
        user._spreading_organization_id = organization.pk

        async def observar():
            with tenant_suspenso(organization.pk, actor_id=user.pk):
                _registrar_veredito_lb(
                    user, "conectado", "campo e botão confirmados",
                )

        asyncio.run(observar())

        record = MercadoLivreSession.objects.get(organization=organization)
        self.assertEqual(record.lb_readiness, "ready")
        self.assertEqual(record.lb_last_probe_result, "conectado")

    def test_lote_sob_tenant_suspenso_le_e_grava_no_tenant_correto(self):
        from apps.accounts.ml_sessions import save_storage_state
        from apps.accounts.tenant import tenant_suspenso
        from apps.scrapers.scraper_mercadolivre import link as ml

        user = get_user_model().objects.create_user("ml-suspenso", password="test")
        organization = user.personal_organization
        save_storage_state(
            user,
            {"cookies": [{"name": "ssid", "value": "x"}], "origins": []},
        )
        produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Item tenant", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=70,
            link_produto="https://produto.mercadolivre.com.br/MLB-998877",
        )

        @contextmanager
        def browser_falso(**_kwargs):
            yield Mock(), Mock()

        async def gerar():
            with tenant_suspenso(organization.pk, actor_id=user.pk):
                return ml.gerar_links_em_lote([produto], usuario=user)

        with patch.object(ml, "iniciar_browser", browser_falso), \
             patch.object(ml, "_abrir_link_builder"), \
             patch.object(
                 ml, "_afiliar_url_na_pagina",
                 return_value="https://meli.la/tenant-certo",
             ):
            resultado = asyncio.run(gerar())

        self.assertEqual(resultado, (1, 0))
        self.assertTrue(LinkAfiliadoUsuario.objects.filter(
            usuario=user, produto=produto,
            link_afiliado="https://meli.la/tenant-certo",
        ).exists())


class StatusDeConexaoComLinkBuilderTests(SimpleTestCase):
    """O bloco `linkbuilder` do JSON que a tela de conexão pinta.

    É o contrato entre ml_conexao.status() e pintarLinkBuilder() no template. O
    campo `medido` existe para a tela render "—" em vez de afirmar algo: sem sessão
    nenhuma, o diagnóstico relevante é `motivo_desconexao`, e dar dois diagnósticos
    para a mesma causa era parte da confusão original.
    """

    def setUp(self):
        cache.clear()

    @staticmethod
    def _estado(conectado, motivo="", alerta="", servico="Link Builder"):
        from apps.scrapers.conexoes import Estado

        return Estado(conectado, servico, "sonda", motivo, "", None, alerta=alerta)

    def _status(self, site, lb):
        from apps.scrapers import ml_conexao

        with patch.object(ml_conexao, "_transport", Mock(status=Mock(return_value={}))), \
             patch("apps.scrapers.conexoes.estado_ml", return_value=site), \
             patch("apps.scrapers.conexoes.estado_ml_linkbuilder",
                   return_value=lb) as sondou, \
             patch("django.contrib.auth.get_user_model") as modelo:
            modelo.return_value.objects.filter.return_value.first.return_value = Mock()
            return ml_conexao.status(1), sondou

    def test_portal_pedindo_login_com_site_ok(self):
        """O caso do relato: site verde, Link Builder recusando."""
        dados, _ = self._status(
            self._estado(True, servico="Mercado Livre"),
            self._estado(False, "O Link Builder do Mercado Livre está pedindo login de novo."),
        )
        self.assertTrue(dados["auth_valido"])
        self.assertEqual(dados["linkbuilder"]["medido"], True)
        self.assertFalse(dados["linkbuilder"]["ok"])
        self.assertIn("Link Builder", dados["linkbuilder"]["motivo"])

    def test_portal_instavel_vira_alerta_e_nao_motivo(self):
        """Suspeita isolada não desconecta: a tela avisa sem alarmar."""
        dados, _ = self._status(
            self._estado(True, servico="Mercado Livre"),
            self._estado(True, alerta="O Link Builder recusou a última verificação."),
        )
        self.assertTrue(dados["linkbuilder"]["ok"])
        self.assertIn("recusou", dados["linkbuilder"]["alerta"])
        self.assertEqual(dados["linkbuilder"]["motivo"], "")

    def test_sem_sessao_nao_sonda_o_portal(self):
        """Sondar o portal sem credencial gastaria um GET para dizer o que a linha
        de cima já diz — e a tela mostraria dois avisos para uma causa."""
        dados, sondou = self._status(
            self._estado(False, "Nenhuma sessão do Mercado Livre.", servico="Mercado Livre"),
            self._estado(True),
        )
        self.assertFalse(dados["auth_valido"])
        self.assertFalse(dados["linkbuilder"]["medido"])
        sondou.assert_not_called()

    def test_login_em_curso_nao_sonda_o_portal(self):
        """Durante o login a fase já manda na tela, e a sonda custa até 8s de rede
        num endpoint que o front chama a cada 3s."""
        from apps.scrapers import ml_conexao

        cache.set(ml_conexao._cache_key(1), {"fase": "validando"})
        with patch.object(ml_conexao, "_transport", Mock(status=Mock(return_value={}))), \
             patch("apps.scrapers.conexoes.estado_ml_linkbuilder") as sondou:
            dados = ml_conexao.status(1)

        self.assertFalse(dados["linkbuilder"]["medido"])
        sondou.assert_not_called()


class MensagemDeSessaoDeLinkTests(SimpleTestCase):
    """Os textos que o usuário lê quando a geração de link falha.

    O texto antigo afirmava que o portal "tem sessão própria, separada da sua
    conta no site" e mandava reconectar em Conexão Mercado Livre. As duas metades
    eram verdadeiras isoladamente e contraditórias juntas: o usuário concluía que
    faltava uma segunda conexão, que não existe — o Link Builder usa a sessão do
    site (iniciar_browser(session_user=...)).
    """

    def test_nao_sugere_uma_segunda_conexao(self):
        from apps.scrapers.scraper_mercadolivre import link

        texto = link.MSG_SESSAO_EXPIRADA.lower()
        self.assertNotIn("sessão própria", texto)
        self.assertNotIn("separada", texto)
        # E diz o que resolve: um login, na tela que já existe.
        self.assertIn("mesma conexão", texto)
        self.assertIn("conexão mercado livre", texto)

    def test_sem_sessao_tem_texto_proprio(self):
        """"Pediu login de novo" para quem nunca conectou era desorientador."""
        from apps.scrapers.scraper_mercadolivre import link

        self.assertNotEqual(link.MSG_SEM_SESSAO, link.MSG_SESSAO_EXPIRADA)
        self.assertIn("nenhuma conta", link.MSG_SEM_SESSAO.lower())

    def test_veredito_do_link_builder_usa_ponte_de_orm_do_playwright(self):
        """O veredito é persistido fora do event loop do Playwright sync.

        `_registrar_veredito_lb` roda com o Playwright sync ativo, que mantém um
        event loop no greenlet da thread atual: tocar o ORM direto dali levanta
        SynchronousOnlyOperation. Toda a gravação tem de passar pela ponte.
        """
        from apps.scrapers.scraper_mercadolivre import link

        usuario = Mock(id=7)
        organization = Mock(pk="11111111-1111-1111-1111-111111111111")
        # Mesma assinatura da ponte real: `organization_id`/`actor_id` são dela,
        # não do callable. Repassá-los para `fn` mascararia a falha, porque
        # `_registrar_veredito_lb` engole exceção de propósito (é telemetria).
        def executar(fn, *args, organization_id=None, actor_id=None, **kwargs):
            return fn(*args, **kwargs)

        with (
            patch.object(link, "executar_no_tenant", side_effect=executar) as ponte,
            patch("apps.accounts.models.organization_for_user",
                  return_value=organization),
            patch(
                "apps.accounts.ml_sessions.registrar_veredito_linkbuilder"
            ) as registrar,
            patch(
                "apps.accounts.ml_sessions.registrar_prontidao_linkbuilder"
            ) as prontidao,
            patch("apps.scrapers.conexoes.invalidar_ml_organization") as invalidar,
        ):
            link._registrar_veredito_lb(usuario, "conectado", "ok")

        ponte.assert_called_once()
        registrar.assert_called_once_with(organization, "conectado", "ok")
        # 'conectado' é o único veredito que pode acender o verde na tela.
        prontidao.assert_called_once_with(organization, "ready", "ok")
        invalidar.assert_called_once_with(organization.pk)


class EstadoMLTests(SimpleTestCase):
    """Tradução do veredito persistido no Estado que a tela renderiza.

    A POLÍTICA (quantas suspeitas até desconectar, e o fato de nunca apagar a
    credencial) é do repositório e está coberta em apps/accounts/tests.py; aqui só
    verificamos a leitura e o cache.
    """

    STATE = {"cookies": [{"name": "ssid", "value": "x"}], "origins": []}

    def setUp(self):
        cache.clear()

    @staticmethod
    def _org(pk="11111111-1111-1111-1111-111111111111"):
        """Organização fake: o estado é chaveado por ORGANIZAÇÃO, não por usuário
        (a sessão ML é OneToOne com ela)."""
        return patch("apps.accounts.models.organization_for_user",
                     return_value=Mock(pk=pk))

    @staticmethod
    def _snapshot(**campos):
        base = {"status": "active", "last_probe_at": None,
                "last_probe_result": "", "probe_failures": 0, "probe_reason": ""}
        base.update(campos)
        return base

    def test_sem_sessao_e_desconectado_com_motivo(self):
        from apps.scrapers.conexoes import estado_ml

        with self._org(), patch("apps.accounts.ml_sessions.probe_snapshot",
                                return_value=None):
            est = estado_ml(Mock(id=7))
        self.assertFalse(est.conectado)
        self.assertEqual(est.detalhe, "sem_sessao")
        self.assertTrue(est.motivo)

    def test_suspeita_isolada_mantem_conectado_com_alerta(self):
        """Uma suspeita não desconecta ninguém: o anti-bot do ML redireciona
        requisições autenticadas vindas do IP da Fly, e derrubar a conexão aí
        desconectava quem tinha acabado de conectar."""
        from apps.scrapers.conexoes import estado_ml

        with (
            self._org(),
            patch("apps.accounts.ml_sessions.probe_snapshot",
                  return_value=self._snapshot()),
            patch("apps.accounts.ml_sessions.load_storage_state", return_value=self.STATE),
            patch("apps.accounts.ml_sessions.registrar_veredito",
                  return_value=self._snapshot(status="suspect", probe_failures=1)),
            patch("apps.scrapers.conexoes.sondar_sessao_ml",
                  return_value=("suspeito", "redirect")),
        ):
            est = estado_ml(Mock(id=7))
        self.assertTrue(est.conectado)
        self.assertFalse(est.motivo)
        self.assertTrue(est.alerta)

    def test_suspeitas_repetidas_pedem_reconexao(self):
        from apps.scrapers.conexoes import estado_ml

        with (
            self._org(),
            patch("apps.accounts.ml_sessions.probe_snapshot",
                  return_value=self._snapshot(status="expired", probe_failures=3,
                                              last_probe_at=timezone.now(),
                                              last_probe_result="suspeito")),
        ):
            est = estado_ml(Mock(id=7))
        self.assertFalse(est.conectado)
        self.assertEqual(est.detalhe, "expirado")

    def test_veredito_fresco_do_banco_nao_sonda_de_novo(self):
        """O veredito é compartilhado pelos nove processos: se um sondou há pouco,
        os outros oito leem em vez de bater no ML de novo."""
        from apps.scrapers.conexoes import estado_ml

        with (
            self._org(),
            patch("apps.accounts.ml_sessions.probe_snapshot",
                  return_value=self._snapshot(last_probe_at=timezone.now(),
                                              last_probe_result="conectado")),
            patch("apps.scrapers.conexoes.sondar_sessao_ml") as sonda,
        ):
            self.assertTrue(estado_ml(Mock(id=7)).conectado)
        sonda.assert_not_called()

    def test_leitor_de_projecao_usa_snapshot_vencido_sem_ir_a_rede(self):
        from apps.scrapers.conexoes import estado_ml

        with (
            self._org(),
            patch("apps.accounts.ml_sessions.probe_snapshot",
                  return_value=self._snapshot(last_probe_at=None,
                                              last_probe_result="conectado")),
            patch("apps.accounts.ml_sessions.load_storage_state") as load,
            patch("apps.scrapers.conexoes.sondar_sessao_ml") as sonda,
        ):
            estado = estado_ml(Mock(id=7), permitir_sonda=False)
        self.assertTrue(estado.conectado)
        load.assert_not_called()
        sonda.assert_not_called()

    def test_conectado_e_cacheado(self):
        """A sonda vai à rede; dashboard e Saúde fazem polling. Sem cache, cada aba
        aberta viraria uma ida ao ML."""
        from apps.scrapers.conexoes import estado_ml

        user = Mock(id=7)
        with (
            self._org(),
            patch("apps.accounts.ml_sessions.probe_snapshot",
                  return_value=self._snapshot()),
            patch("apps.accounts.ml_sessions.load_storage_state", return_value=self.STATE),
            patch("apps.accounts.ml_sessions.registrar_veredito",
                  return_value=self._snapshot(last_probe_result="conectado")),
            patch("apps.scrapers.conexoes.sondar_sessao_ml",
                  return_value=("conectado", "")) as sonda,
        ):
            estado_ml(user)
            estado_ml(user)
            estado_ml(user)
        self.assertEqual(sonda.call_count, 1)

    def test_sondar_nao_marca_a_sessao_como_usada(self):
        """`last_used_at` responde 'quando a sessão trabalhou', e uma tela aberta
        não é trabalho — era isso que virava um UPDATE a cada poll de 3s."""
        from apps.scrapers.conexoes import estado_ml

        with (
            self._org(),
            patch("apps.accounts.ml_sessions.probe_snapshot",
                  return_value=self._snapshot()),
            patch("apps.accounts.ml_sessions.load_storage_state",
                  return_value=self.STATE) as load,
            patch("apps.accounts.ml_sessions.registrar_veredito",
                  return_value=self._snapshot()),
            patch("apps.scrapers.conexoes.sondar_sessao_ml",
                  return_value=("conectado", "")),
        ):
            estado_ml(Mock(id=7))
        self.assertFalse(load.call_args.kwargs["touch"])


@override_settings(
    WHATSAPP_API_URL="http://whatsapp.internal:3000",
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
        _mock_wa_capability(self)

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
            headers=_TEST_WA_HEADERS,
            params=None, json={"session": "user-42"},
            timeout=(whatsapp_client._TIMEOUT_CONNECT_S, 25),
        )


class WhatsAppIsolationTests(SimpleTestCase):
    def setUp(self):
        _mock_wa_capability(self)

    @patch("apps.scrapers.whatsapp_client.status")
    def test_connection_monitor_checks_the_requested_session(self, status):
        status.return_value = {"conectado": True}
        self.assertTrue(wa_conectado("user-42"))
        status.assert_called_once_with("user-42")

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
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
            headers=_TEST_WA_HEADERS,
            params=None, json={"session": "user-42"},
            timeout=(whatsapp_client._TIMEOUT_CONNECT_S, 10),
        )

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
    )
    @patch("apps.scrapers.whatsapp_client.requests.post")
    def test_send_routes_to_the_users_session(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {"sucesso": True, "mensagem_id": "abc123"}
        post.return_value = response

        result = whatsapp_client.enviar_oferta(
            "123@g.us", "mensagem", session="user-42",
            idempotency_key="publicacao:123",
        )

        self.assertTrue(result["sucesso"])
        self.assertEqual(post.call_args.kwargs["json"]["session"], "user-42")
        self.assertEqual(post.call_args.kwargs["json"]["grupoid"], "123@g.us")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Idempotency-Key"],
            "publicacao:123",
        )
        self.assertEqual(post.call_args.kwargs["timeout"], 65)

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
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

    def setUp(self):
        _mock_wa_capability(self)

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
    )
    @patch("apps.scrapers.whatsapp_client.requests.request")
    def test_unreachable_worker_is_reported_as_erro(self, request):
        request.side_effect = OSError("connection refused")
        self.assertIn("erro", whatsapp_client.listar_grupos("user-42"))

    @override_settings(
        WHATSAPP_API_URL="http://whatsapp.internal:3000",
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
    )
    @patch("apps.scrapers.whatsapp_client.requests.request")
    def test_logout_does_not_retry(self, request):
        response = Mock()
        response.json.return_value = {"sucesso": True}
        request.return_value = response

        whatsapp_client.desconectar("user-42")

        request.assert_called_once_with(
            "POST", "http://whatsapp.internal:3000/api/sessoes/logout",
            headers=_TEST_WA_HEADERS,
            params=None, json={"session": "user-42"},
            timeout=(whatsapp_client._TIMEOUT_CONNECT_S, 25),
        )


class WhatsAppErrorTaxonomyTests(SimpleTestCase):
    """Toda falha de envio carrega `classe`. O orquestrador decide por ela se
    conta a falha contra a regra do usuário — ver EnvioResilienciaTests."""

    def setUp(self):
        _mock_wa_capability(self)

    def _post(self, **kwargs):
        return patch("apps.scrapers.whatsapp_client.requests.post", **kwargs)

    @override_settings(WHATSAPP_API_URL="http://wa.internal:3000")
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

    @override_settings(WHATSAPP_API_URL="http://wa.internal:3000")
    def test_timeout_and_refused_connection_are_transient(self):
        # Os dois piores casos nunca chegam classificados pelo Node — ele não
        # chegou a responder. São exatamente os que desligavam a automação.
        for erro in (requests.Timeout("read timeout"),
                     requests.ConnectionError("connection refused")):
            with self.subTest(erro=type(erro).__name__), self._post(side_effect=erro):
                r = whatsapp_client.enviar_oferta("123@g.us", "m", session="u")
            self.assertFalse(r["sucesso"])
            self.assertEqual(r["classe"], "transitorio")

    @override_settings(WHATSAPP_API_URL="http://wa.internal:3000")
    def test_rate_limit_is_transient_and_bad_request_is_permanent(self):
        casos = [(429, "transitorio"), (500, "transitorio"), (400, "permanente")]
        for status, esperado in casos:
            response = Mock(status_code=status)
            response.json.return_value = {"erro": "x"}   # Node antigo: sem classe
            with self.subTest(status=status), self._post(return_value=response):
                r = whatsapp_client.enviar_oferta("123@g.us", "m", session="u")
            self.assertEqual(r["classe"], esperado)

    @override_settings(WHATSAPP_API_URL="http://wa.internal:3000")
    def test_node_regression_does_not_punish_the_user(self):
        # sucesso sem mensagem_id é bug nosso, não da configuração dele.
        response = Mock(status_code=200)
        response.json.return_value = {"sucesso": True}
        with self._post(return_value=response):
            r = whatsapp_client.enviar_oferta("123@g.us", "m", session="u")
        self.assertFalse(r["sucesso"])
        self.assertEqual(r["classe"], "transitorio")

    @override_settings(WHATSAPP_API_URL="http://wa.internal:3000")
    def test_corpo_do_worker_e_redigido_e_limitado_a_campos_conhecidos(self):
        response = Mock(status_code=503)
        response.json.return_value = {
            "sucesso": True,
            "erro": "Bearer capability-secret cookie=session-secret",
            "classe": "transitorio",
            "base64": "A" * 400,
            "payload": {"token": "opaque-secret"},
        }
        with self._post(return_value=response):
            result = whatsapp_client.enviar_oferta(
                "123@g.us", "mensagem", session="u",
            )

        serialized = json.dumps(result)
        self.assertFalse(result["sucesso"])
        self.assertNotIn("capability-secret", serialized)
        self.assertNotIn("session-secret", serialized)
        self.assertNotIn("opaque-secret", serialized)
        self.assertNotIn("base64", result)
        self.assertNotIn("payload", result)

    @override_settings(WHATSAPP_API_URL="http://wa.internal:3000")
    def test_excecao_de_transporte_nao_ecoa_segredo(self):
        with self._post(side_effect=RuntimeError(
            "https://wa.internal/send?token=secret cookie=session-secret"
        )):
            result = whatsapp_client.enviar_oferta(
                "123@g.us", "mensagem", session="u",
            )
        serialized = json.dumps(result)
        self.assertNotIn("token=secret", serialized)
        self.assertNotIn("session-secret", serialized)
        self.assertEqual(result["causa"], "RuntimeError")


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
        # A contrapartida: se o grupo sumiu, insistir só martela o WhatsApp. O freio
        # agora é `pausada_ate`, não `ativo=False` — `ativo` voltou a ser exclusivo
        # do usuário, e o freio expira sozinho (ver test_the_pause_expires_...).
        falha = {"sucesso": False, "motivo": "Grupo de destino nao encontrado.",
                 "classe": "permanente"}
        for _ in range(self.cfg.pausar_apos_falhas):
            self.cfg.proximo_envio = None
            self.cfg.save(update_fields=["proximo_envio"])
            self._processar({"conectado": True}, envio=falha)

        self.assertTrue(self.cfg.ativo, "o freio automático não desliga a regra")
        self.assertIsNotNone(self.cfg.pausada_ate)
        self.assertGreater(self.cfg.pausada_ate, timezone.now())
        self.assertIn("Grupo de destino", self.cfg.motivo_pausa)

    def test_the_pause_expires_and_the_rule_tries_again(self):
        # O defeito relatado pela cliente: "programei e durou só ontem". Antes o
        # freio era definitivo e ninguém avisava; agora ele vence e a regra volta.
        falha = {"sucesso": False, "motivo": "Grupo de destino nao encontrado.",
                 "classe": "permanente"}
        for _ in range(self.cfg.pausar_apos_falhas):
            self.cfg.proximo_envio = None
            self.cfg.save(update_fields=["proximo_envio"])
            self._processar({"conectado": True}, envio=falha)
        self.assertIsNotNone(self.cfg.pausada_ate)

        # Prazo vencido: o próximo tick solta o freio e tenta de novo.
        ConfiguracaoEnvio.objects.filter(pk=self.cfg.pk).update(
            pausada_ate=timezone.now() - timedelta(minutes=1), proximo_envio=None)
        _st, _cli, enviar, _ = self._processar(
            {"conectado": True}, envio={"sucesso": True, "via": "whatsapp"})

        enviar.assert_called()
        self.assertIsNone(self.cfg.pausada_ate)
        self.assertEqual(self.cfg.falhas_consecutivas, 0)
        self.assertEqual(self.cfg.motivo_pausa, "")

    def test_unclassified_failure_is_infrastructure_and_does_not_pause(self):
        # Node antigo/throw minificado é incerto: não desliga uma regra válida.
        falha = {"sucesso": False, "motivo": "erro estranho"}
        for _ in range(self.cfg.pausar_apos_falhas):
            self.cfg.proximo_envio = None
            self.cfg.save(update_fields=["proximo_envio"])
            self._processar({"conectado": True}, envio=falha)

        self.assertIsNone(self.cfg.pausada_ate)


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

    def test_accepts_whole_macro_niche_in_a_single_rule(self):
        """Marcar o macro-nicho inteiro numa regra só tem de funcionar.

        Os sub-nichos de Eletrodomésticos somam 395 caracteres. Com o CharField(255)
        isso primeiro derrubava a tela com 500 ("value too long for type character
        varying(255)") e depois passou a ser recusado com uma mensagem — mas as duas
        saídas obrigavam o afiliado a criar uma regra por sub-nicho para o MESMO
        grupo, que é justamente o que ele pediu para não precisar fazer.
        """
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import SUBNICHOS

        termos = [termos for _, termos in SUBNICHOS["Eletrodomésticos"]]
        self.assertGreater(len(", ".join(termos)), 255)  # o caso que quebrava

        response = self.client.post(self.url, {
            "canal": "whatsapp",
            "grupo_id": "123@g.us",
            "macro_categoria": "Eletrodomésticos",
            "termo_busca": termos,
            "intervalo_minutos": "60",
            "min_desconto_percent": "15",
        })

        self.assertRedirects(response, self.url)
        cfg = self.user.configuracoes.get()
        self.assertEqual(cfg.termo_busca, ", ".join(termos))

    def test_accepts_every_subniche_of_every_macro_niche(self):
        """O teto de sanidade não pode alcançar o uso legítimo mais extremo."""
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import SUBNICHOS

        termos = [t for itens in SUBNICHOS.values() for _, t in itens]

        response = self.client.post(self.url, {
            "canal": "whatsapp",
            "grupo_id": "123@g.us",
            "termo_busca": termos,
            "intervalo_minutos": "60",
            "min_desconto_percent": "15",
        })

        self.assertRedirects(response, self.url)
        cfg = self.user.configuracoes.get()
        # Cada sub-nicho já é uma lista de sinônimos ("aspirador robo, robot
        # vacuum, ..."), então os 70 sub-nichos viram bem mais termos que isso —
        # e é a contagem final que o filtro de envio percorre em OU.
        self.assertEqual(cfg.termo_busca, ", ".join(termos))
        individuais = [t.strip() for t in cfg.termo_busca.split(",") if t.strip()]
        self.assertGreater(len(individuais), len(termos))

    def test_rejects_forged_subniche_list(self):
        """POST forjado ainda é recusado — e sem 500."""
        from apps.scrapers.views import LIMITE_TERMOS_POR_REGRA

        response = self.client.post(self.url, {
            "canal": "whatsapp",
            "grupo_id": "123@g.us",
            "termo_busca": ["x" * (LIMITE_TERMOS_POR_REGRA + 1)],
            "intervalo_minutos": "60",
            "min_desconto_percent": "15",
        })

        self.assertRedirects(response, self.url)
        self.assertFalse(self.user.configuracoes.exists())
        self.assertTrue(any(
            "longa demais" in str(message)
            for message in get_messages(response.wsgi_request)
        ))

    def test_accepts_subniches_that_fit(self):
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import SUBNICHOS

        termos = [termos for _, termos in SUBNICHOS["Eletrodomésticos"][:3]]

        response = self.client.post(self.url, {
            "canal": "whatsapp",
            "grupo_id": "123@g.us",
            "macro_categoria": "Eletrodomésticos",
            "termo_busca": termos,
            "intervalo_minutos": "60",
            "min_desconto_percent": "15",
        })

        self.assertRedirects(response, self.url)
        cfg = self.user.configuracoes.get()
        self.assertEqual(cfg.termo_busca, ", ".join(termos))

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
        link = f"https://meli.la/{produto.id}"
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto,
            link_afiliado=link, url_isca=produto.link_produto,
            afiliado_ok=True, estado="pronto",
            # Enviável = destino já verificado. A fixture representa um item pronto.
            verificado_ok=True, url_canonica=link,
        )
        return produto

    def test_item_sem_categoria_vira_opcao_em_vez_de_sumir(self):
        """Escolher qualquer subcategoria descartava calado quem não tem categoria.

        Como a maior fonte do catálogo grava 'DESCONHECIDO', isso deixava a lista de
        subcategorias vazia e, quando havia alguma, sumia com a loja inteira.
        """
        from apps.scrapers.views import SEM_SUBCATEGORIA

        sem_cat = self._criar_produto(
            marketplace="mercadolivre", nome="Robô Aspirador",
            categoria="DESCONHECIDO", macro_categoria="Eletrônicos",
            preco_sem_desconto=200, preco_com_cupom=100,
            link_produto="https://example.com/robo",
        )

        response = self.client.get(self.url, {"macro": "Eletrônicos"})
        self.assertIn(SEM_SUBCATEGORIA,
                      response.context["categorias_por_macro"]["Eletrônicos"])

        response = self.client.get(
            self.url, {"macro": "Eletrônicos", "categoria": SEM_SUBCATEGORIA})
        self.assertEqual([p.nome for p in response.context["produtos"]], [sem_cat.nome])

    def test_subcategoria_real_nao_traz_os_sem_categoria(self):
        """A opção nova não pode virar um coringa que ignora o filtro."""
        self._criar_produto(
            marketplace="mercadolivre", nome="Robô Aspirador",
            categoria="DESCONHECIDO", macro_categoria="Eletrônicos",
            preco_sem_desconto=200, preco_com_cupom=100,
            link_produto="https://example.com/robo",
        )

        response = self.client.get(
            self.url, {"macro": "Eletrônicos", "categoria": "Áudio"})

        self.assertEqual([p.nome for p in response.context["produtos"]],
                         ["Fone Bluetooth"])

    def test_categoria_nula_conta_como_sem_subcategoria(self):
        """As três formas de 'ninguém classificou' têm de cair na mesma opção."""
        from apps.scrapers.views import SEM_SUBCATEGORIA

        nulo = self._criar_produto(
            marketplace="mercadolivre", nome="Ventilador", categoria=None,
            macro_categoria="Eletrônicos", preco_sem_desconto=200,
            preco_com_cupom=100, link_produto="https://example.com/ventilador",
        )

        response = self.client.get(
            self.url, {"macro": "Eletrônicos", "categoria": SEM_SUBCATEGORIA})

        self.assertEqual([p.nome for p in response.context["produtos"]], [nulo.nome])

    def test_catalogo_de_cupons_mostra_so_prontos_e_indica_fila(self):
        # `cupons_coletados` e companhia são contadores por ETAPA da esteira: hoje só
        # o admin os recebe no contexto (ver `diagnostico` em `top_promocoes`). A
        # lista de cupons em si, checada no fim, vale para qualquer usuário.
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        from apps.accounts.models import MercadoLivreSession
        from apps.scrapers.coupon_products import atualizar_chave_cupom
        from apps.scrapers.models import (
            CupomDisponibilidade, CupomPreparacao,
            LinkAfiliadoCupomUsuario, ProdutoCupom,
        )

        fonte = FonteIngestao.objects.create(
            slug="coupon-counter-source", marketplace="mercadolivre", nome="Cupons")
        pronto = CupomNormalizado.objects.create(
            fonte=fonte, external_id="counter-ready", marketplace="mercadolivre",
            titulo="Cupom pronto", codigo="PRONTO20",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 20}, estado="ativo",
        )
        aguardando = CupomNormalizado.objects.create(
            fonte=fonte, external_id="counter-pending", marketplace="mercadolivre",
            titulo="Cupom aguardando", codigo="AGUARDA20",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 20}, estado="ativo",
        )
        MercadoLivreSession.objects.create(
            organization=self.user.perfil.organization, key_version="v1",
            wrapped_dek=b"wrapped", wrap_nonce=b"wrap", data_nonce=b"data",
            ciphertext=b"cipher", status="active", lb_readiness="ready",
        )
        LinkAfiliadoCupomUsuario.objects.create(
            usuario=self.user, cupom=pronto, url_origem=pronto.link or "https://example.com",
            link_afiliado="https://meli.la/cupom-pronto", afiliado_ok=True,
        )
        CupomPreparacao.objects.create(
            cupom=pronto, usuario=None, status="pronto",
            produtos_chave=atualizar_chave_cupom(pronto), verificado_em=timezone.now(),
        )
        produto = self._criar_produto(
            marketplace="mercadolivre", owner=None, nome="Produto do cupom",
            preco_sem_desconto=100, preco_com_cupom=80,
            link_produto="https://example.com/produto-cupom",
            imagem_url="https://img.example/produto-cupom.jpg",
        )
        ProdutoCupom.objects.create(
            cupom=pronto, produto=produto, status="confirmado",
            preco_original=100, preco_atual=80, preco_final=60,
            verificado_em=timezone.now(),
        )
        # A view é somente leitura: quem materializa esta projeção é o worker de
        # cupons. O fixture precisa reproduzir esse contrato em vez de esperar que
        # um GET escreva no banco (comportamento removido em 816ccee).
        CupomDisponibilidade.objects.create(
            organization=self.user.perfil.organization,
            usuario=self.user, cupom=pronto,
            use_mode="code_notice", stage="ready",
        )
        CupomDisponibilidade.objects.create(
            organization=self.user.perfil.organization,
            usuario=self.user, cupom=aguardando,
            use_mode="code_notice", stage="waiting_link",
            category="no_link", reason_code="affiliate_link_pending",
        )

        response = self.client.get(self.url, {"tipo": "cupom"})

        self.assertEqual(response.context["cupons_coletados"], 2)
        self.assertEqual(response.context["cupons_prontos"], 1)
        self.assertEqual(response.context["cupons_aguardando_preparo"], 0)
        self.assertEqual(response.context["cupons_aguardando_link"], 1)
        # REGRA DA TELA: só entra na lista o que pode ser enviado. O cupom em
        # `waiting_link` aparecia aqui e oferecia um envio que o funil não
        # sustentava — era o "aguardando link" que o usuário via na tela.
        self.assertEqual(
            {c.id for c in response.context["cupons_catalogo"]},
            {pronto.id},
        )
        exibido = response.context["cupons_catalogo"][0]
        self.assertEqual(exibido.evidencia_rotulo, "Fonte estruturada")
        self.assertEqual(exibido.evidencia_fontes, 1)
        self.assertContains(response, "Atualizado há")
        self.assertContains(response, "gates de escopo, produto, preço e link")
        # Some da lista, mas não do conhecimento: vira contador.
        self.assertEqual(response.context["cupons_em_preparo"], 1)

    def test_coupon_without_validity_older_than_ttl_is_not_collected(self):
        fonte = FonteIngestao.objects.create(
            slug="coupon-stale-source", marketplace="amazon", nome="Cupons antigos")
        antigo = CupomNormalizado.objects.create(
            fonte=fonte, external_id="old-no-expiry", marketplace="amazon",
            titulo="Cupom sem validade que sumiu da fonte", codigo="VELHO20",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 20}, estado="ativo",
        )
        CupomNormalizado.objects.filter(pk=antigo.pk).update(
            ultima_observacao=timezone.now() - timedelta(hours=49))

        response = self.client.get(self.url, {"tipo": "cupom"})

        self.assertEqual(response.context["cupons_coletados"], 0)

    def test_search_and_minimum_discount_are_applied(self):
        response = self.client.get(self.url, {"q": "fone", "min_desconto": "40"})

        self.assertEqual([p.nome for p in response.context["produtos"]], ["Fone Bluetooth"])

    def test_busca_ignora_acento_e_caixa(self):
        """Buscar "robô" só achava produto do ML e o cliente achava que faltava item.

        `icontains` vira ILIKE no Postgres, que é sensível a acento: "robo" não
        casava com nenhum título que traz "robô". Toda grafia tem de devolver o
        mesmo conjunto — inclusive o item da Amazon, que é privado do usuário.
        """
        self._criar_produto(
            marketplace="amazon", owner=self.user,
            nome="Robô Aspirador Inteligente", categoria="Casa",
            macro_categoria="Eletrodomésticos", preco_sem_desconto=200,
            preco_com_cupom=100, link_produto="https://example.com/robo",
        )

        for termo in ("robô", "robo", "ROBÔ", "Robo", "aspirador"):
            with self.subTest(termo=termo):
                response = self.client.get(self.url, {"q": termo})
                self.assertEqual(
                    [p.nome for p in response.context["produtos"]],
                    ["Robô Aspirador Inteligente"],
                )

    def test_nome_normalizado_acompanha_alteracao_do_titulo(self):
        """Raspagem que só atualiza `nome` não pode deixar a busca no título antigo."""
        produto = self._criar_produto(
            marketplace="mercadolivre", nome="Ventilador",
            categoria="Casa", macro_categoria="Eletrodomésticos",
            preco_sem_desconto=100, preco_com_cupom=50,
            link_produto="https://example.com/ventilador",
        )

        produto.nome = "Climatizador Portátil"
        produto.save(update_fields=["nome"])

        response = self.client.get(self.url, {"q": "portatil"})
        self.assertEqual(
            [p.nome for p in response.context["produtos"]], ["Climatizador Portátil"])

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

    def test_active_product_older_than_catalog_ttl_is_hidden(self):
        old = self._criar_produto(
            marketplace="mercadolivre", nome="Oferta ativa mas vencida",
            categoria="Cozinha", macro_categoria="Casa",
            preco_sem_desconto=100, preco_com_cupom=50,
            link_produto="https://produto.mercadolivre.com.br/MLB-9000001",
            estado="ativo",
        )
        Produto.objects.filter(pk=old.pk).update(
            ultima_observacao=timezone.now() - timedelta(hours=49))

        response = self.client.get(self.url, {"q": "Oferta ativa mas vencida"})

        self.assertNotIn(old.id, [p.id for p in response.context["produtos"]])

    def test_discount_at_or_above_ninety_percent_is_hidden(self):
        incoerente = self._criar_produto(
            marketplace="amazon", owner=self.user,
            nome="Preço de referência em escala errada",
            categoria="Outros", macro_categoria="Outros",
            preco_sem_desconto=639.90, preco_com_cupom=63.99,
            link_produto="https://example.com/preco-incoerente",
        )

        response = self.client.get(
            self.url, {"q": "Preço de referência em escala errada"})

        self.assertNotIn(
            incoerente.id, [p.id for p in response.context["produtos"]])

    def test_product_without_real_discount_is_hidden(self):
        sem_desconto = self._criar_produto(
            marketplace="amazon", owner=self.user,
            nome="Produto com cupom sem abatimento confirmado",
            categoria="Outros", macro_categoria="Outros",
            preco_sem_desconto=2111.10, preco_com_cupom=2111.10,
            link_produto="https://example.com/sem-desconto",
        )

        response = self.client.get(
            self.url, {"q": "Produto com cupom sem abatimento confirmado"})

        self.assertNotIn(
            sem_desconto.id, [p.id for p in response.context["produtos"]])

    def test_same_ml_item_and_title_uses_only_latest_observation(self):
        first = self._criar_produto(
            marketplace="mercadolivre", nome="Top Puma repetido",
            categoria="Moda", macro_categoria="Moda",
            preco_sem_desconto=160, preco_com_cupom=38,
            link_produto=("https://produto.mercadolivre.com.br/MLB-3102506128-item"
                          "?searchVariation=111"),
        )
        second = self._criar_produto(
            marketplace="mercadolivre", nome="Top Puma repetido",
            categoria="Moda", macro_categoria="Moda",
            preco_sem_desconto=160, preco_com_cupom=40,
            link_produto=("https://produto.mercadolivre.com.br/MLB-3102506128-item"
                          "?searchVariation=222"),
        )
        Produto.objects.filter(pk=first.pk).update(
            ultima_observacao=timezone.now() - timedelta(hours=1))

        response = self.client.get(self.url, {"q": "Top Puma repetido"})

        self.assertEqual(
            [p.id for p in response.context["produtos"]], [second.id])

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
                         ["amazon-public-web", "mercadolivre-web"])

    def test_lane_desligada_nao_e_apresentada_como_fonte_parada(self):
        """"Parada" e "desligada" pedem ações opostas.

        Em produção as três fontes da lane de raspagem apareciam como "Sem coleta
        há mais de 8h" porque a flag estava desligada desde a véspera — a faixa
        mandava procurar defeito numa coleta que ninguém tinha mandado rodar.
        """
        # A faixa de diagnóstico é de admin (ver `diagnostico` na view).
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        FonteIngestao.objects.filter(slug="mercadolivre-web").update(
            status="ok", ultima_tentativa=timezone.now() - timedelta(days=2))

        with patch("apps.scrapers.automacao_state.is_enabled", return_value=False):
            response = self.client.get(self.url)

        fonte = next(f for f in response.context["fontes"]
                     if f.slug == "mercadolivre-web")
        self.assertEqual(fonte.status_exibicao, "off")
        self.assertIn("tela Scraper", fonte.motivo_exibicao)
        self.assertTrue(any("Raspagem desligada" in aviso["nome"]
                            for aviso in response.context["fontes_com_aviso"]))

    def test_lane_ligada_com_fonte_muda_continua_parada(self):
        """Com a lane ligada, silêncio longo volta a ser o defeito que é."""
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        FonteIngestao.objects.filter(slug="mercadolivre-web").update(
            status="ok", ultima_tentativa=timezone.now() - timedelta(days=2))

        with patch("apps.scrapers.automacao_state.is_enabled", return_value=True):
            response = self.client.get(self.url)

        fonte = next(f for f in response.context["fontes"]
                     if f.slug == "mercadolivre-web")
        self.assertEqual(fonte.status_exibicao, "silent")
        self.assertIn("Sem coleta", fonte.motivo_exibicao)

    @patch("apps.scrapers.scraper_mercadolivre.ofertas_scraper.mapear_ofertas",
           return_value=12)
    @patch("apps.scrapers.coupon_pipeline._coletar_adaptador")
    @patch("apps.scrapers.coupon_products.preparar_lote",
           return_value={"processados": 0, "prontos": 0})
    def test_flash_scrape_does_not_mask_full_mercado_livre_source(
            self, _preparo, collect_radar, _mapear):
        source = FonteIngestao.objects.get(slug="mercadolivre-web")
        source.status = "degraded"
        source.falhas_consecutivas = 2
        source.erro_publico = "timeout"
        source.save()
        from apps.scrapers.management.commands.automacao import _rodar_scrape_rapido

        self.assertEqual(_rodar_scrape_rapido(paginas=2), 12)
        self.assertEqual(
            [call.args[0] for call in collect_radar.call_args_list],
            ["ml-lightning-coupons", "pelando-cupons", "telegram-publico"],
        )
        telegram_call = collect_radar.call_args_list[-1]
        self.assertEqual(telegram_call.kwargs["items"], ("coupons",))
        self.assertFalse(telegram_call.kwargs["include_offers"])
        source.refresh_from_db()
        self.assertEqual(source.status, "degraded")
        self.assertEqual(source.falhas_consecutivas, 2)
        flash = FonteIngestao.objects.get(slug="mercadolivre-ofertas-flash")
        self.assertEqual(flash.status, "ok")
        self.assertEqual(flash.ultimo_total, 12)

    @patch(
        "apps.scrapers.scraper_mercadolivre.ofertas_scraper.mapear_ofertas",
        side_effect=BrowserResourceUnavailable("ocupado"),
    )
    @patch("apps.scrapers.coupon_pipeline._coletar_adaptador")
    def test_flash_browser_ocupado_preserva_radares_e_snapshot(
            self, collect_radar, _mapear):
        flash, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-ofertas-flash",
            defaults={
                "marketplace": "mercadolivre", "nome": "Flash", "status": "ok",
                "ultimo_total": 9,
            },
        )
        flash.status = "ok"
        flash.ultimo_total = 9
        flash.save(update_fields=["status", "ultimo_total"])
        from apps.scrapers.management.commands.automacao import _rodar_scrape_rapido

        self.assertIsNone(_rodar_scrape_rapido(paginas=2))
        self.assertEqual(
            [call.args[0] for call in collect_radar.call_args_list],
            ["ml-lightning-coupons", "pelando-cupons", "telegram-publico"],
        )
        flash.refresh_from_db()
        self.assertEqual(flash.status, "ok")
        self.assertEqual(flash.ultimo_total, 9)


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

        try:
            raise RuntimeError(
                "Bearer capability-secret cookie=session-secret "
                "https://portal.example/report?token=query-secret"
            )
        except RuntimeError as exc:
            log_event(
                "sistema", "secret_test",
                "Falha com password=message-secret",
                usuario=self.user,
                contexto={
                    "api_key": "super-secret", "safe": "ok",
                    "detail": "Authorization: context-secret",
                },
                exc=exc,
            )

        event = EventoOperacional.objects.get(evento="secret_test")
        self.assertEqual(event.contexto["api_key"], "***")
        self.assertEqual(event.contexto["safe"], "ok")
        serialized = json.dumps({
            "message": event.mensagem,
            "context": event.contexto,
            "error": event.erro,
        })
        for secret in (
            "message-secret", "context-secret", "capability-secret",
            "session-secret", "query-secret",
        ):
            self.assertNotIn(secret, serialized)

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

    @patch("apps.scrapers.relatorios.report_prerequisites", return_value={
        "ok": True, "code": "ready", "url": "https://portal.example/report",
        "instruction": "",
    })
    @patch("apps.scrapers.relatorios.sync_marketplace")
    def test_botao_sincronizar_agenda_e_nao_executa_no_request(
            self, sync_marketplace, _prerequisites):
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

    @patch("apps.scrapers.ofertas._baixar_imagem_b64", return_value=(None, None))
    @patch("apps.scrapers.senders.registry.get_sender")
    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_produto_publico_nao_tenta_lock_de_escrita(
        self, get_marketplace, get_sender, _image
    ):
        """RLS deixa o catálogo do ML legível, mas não permite FOR UPDATE nele."""
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

        with patch.object(Produto.objects, "select_for_update",
                          side_effect=AssertionError("produto público não pode ser bloqueado")):
            result = enviar_oferta_de_produto(
                self.product, "group@g.us", verificar=False,
                usuario=self.user, destino_nome="Grupo")

        self.assertTrue(result["sucesso"])

    @patch("apps.scrapers.ofertas._baixar_imagem_b64", return_value=(None, None))
    @patch("apps.scrapers.senders.registry.get_sender")
    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_produto_removido_entre_tela_e_reserva_tem_erro_claro(
        self, get_marketplace, get_sender, _image
    ):
        from apps.scrapers.ofertas import enviar_oferta_de_produto
        from apps.scrapers.senders.base import WhatsAppMarkup

        get_marketplace.return_value = Mock()
        get_sender.return_value = Mock(markup=WhatsAppMarkup(), prefers_image="b64")
        exibido = self.product
        self.product.delete()

        result = enviar_oferta_de_produto(
            exibido, "group@g.us", verificar=False, usuario=self.user,
            destino_nome="Grupo")

        self.assertFalse(result["sucesso"])
        self.assertTrue(result["produto_atualizado"])
        self.assertIn("Atualize a tela", result["motivo"])
        self.assertFalse(Publicacao.objects.filter(usuario=self.user).exists())

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

    def test_default_message_uses_configured_cta_brand_and_disclosure(self):
        from apps.scrapers.ofertas import montar_mensagem

        config = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="copy@g.us", nome_marca="Achados da Lu",
            chamada_acao="Ver preço na loja",
            divulgacao_afiliado="Link de afiliado; posso receber comissão.",
        )
        message = montar_mensagem(
            self.product, "https://example.com/a", None,
            usuario=self.user, configuracao=config,
        )

        self.assertIn("Ver preço na loja", message)
        self.assertIn("Achados da Lu", message)
        self.assertIn("Link de afiliado; posso receber comissão.", message)

    def test_custom_template_does_not_claim_unproven_discount_and_escapes_data(self):
        from apps.scrapers.ofertas import montar_mensagem
        from apps.scrapers.senders.base import TelegramHTMLMarkup

        self.product.nome = "TV <script>alert(1)</script>"
        config = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="template@g.us",
            template_a="{nome}|{desconto}|{link}",
        )
        message = montar_mensagem(
            self.product, "https://example.com/a?x=1&y=2", None,
            markup=TelegramHTMLMarkup(), usuario=self.user, configuracao=config,
        )

        self.assertIn("&lt;script&gt;", message)
        self.assertIn("x=1&amp;y=2", message)
        self.assertNotIn("<script>", message)
        self.assertNotIn("50%", message)

    def test_flash_label_only_appears_for_real_flash_offer(self):
        from apps.scrapers.ofertas import montar_mensagem

        comum = montar_mensagem(self.product, "https://example.com/a", None)
        self.product.relampago = True
        relampago = montar_mensagem(self.product, "https://example.com/a", None)

        self.assertNotIn("OFERTA RELÂMPAGO", comum)
        self.assertIn("OFERTA RELÂMPAGO", relampago)

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


class EnviarCupomColagemTests(TestCase):
    """Regressão: no caminho da colagem (itens_cupom) o retorno de sucesso do
    `enviar_cupom` não pode referenciar `afiliado` — era um UnboundLocalError que
    estourava DEPOIS do envio, marcando como falha uma mensagem já entregue."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("lojista", password="test")
        self.user.perfil.marcar_verificado()
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML web"})
        self.cupom = CupomNormalizado.objects.create(
            fonte=fonte, external_id="campanha:999", marketplace="mercadolivre",
            titulo="20% OFF em Moda", estado="ativo",
        )
        self.produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Tênis de corrida", origem="oferta",
            preco_sem_desconto=200, preco_com_cupom=120,
            link_produto="https://example.com/p", link_afiliado="https://meli.la/abc",
            imagem_url="https://img/x.jpg",
        )
        from apps.scrapers.coupon_products import atualizar_chave_cupom
        from apps.scrapers.models import CupomPreparacao, ProdutoCupom
        ProdutoCupom.objects.create(
            produto=self.produto, cupom=self.cupom, status="confirmado",
            preco_original=200, preco_atual=120, preco_final=96,
            verificado_em=timezone.now(),
        )
        CupomPreparacao.objects.create(
            cupom=self.cupom, usuario=None, status="pronto",
            produtos_chave=atualizar_chave_cupom(self.cupom),
            verificado_em=timezone.now(),
        )
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=self.produto, afiliado_ok=True,
            estado="pronto", link_afiliado="https://meli.la/abc",
            verificado_ok=True, verificado_em=timezone.now(),
            url_canonica="https://meli.la/abc",
        )

    @patch("apps.scrapers.ofertas._canal_pronto_ou_erro", return_value=None)
    @patch("apps.scrapers.colagem.montar_colagem_itens")
    @patch("apps.scrapers.ofertas._preparar_itens_cupom")
    @patch("apps.scrapers.senders.registry.get_sender")
    def test_caminho_colagem_retorna_link_do_item_sem_unbound(
        self, get_sender, prep, colagem, _canal
    ):
        from apps.scrapers.senders.base import WhatsAppMarkup
        itens = [{"produto": self.produto, "link": "https://meli.la/abc"}]
        prep.return_value = (itens, False)
        colagem.return_value = ("b64", "image/jpeg", itens)
        sender = Mock(markup=WhatsAppMarkup(), prefers_image="b64")
        sender.enviar_oferta.return_value = {"sucesso": True, "via": "test"}
        get_sender.return_value = sender

        result = ofertas.enviar_cupom(
            self.cupom, "group@g.us", usuario=self.user, destino_nome="Grupo")

        # Sem o fix, isto levantava UnboundLocalError e caía em "Falha inesperada".
        self.assertTrue(result["sucesso"])
        self.assertEqual(result["link"], "https://meli.la/abc")
        _, kwargs = sender.enviar_oferta.call_args
        self.assertEqual(kwargs.get("imagem_b64"), "b64")
        self.assertIn("Tênis de corrida", kwargs.get("legenda", ""))
        self.assertEqual(
            Publicacao.objects.get(cupom_normalizado=self.cupom).status, "enviado")

    def test_categoria_escolhida_nao_prova_aplicabilidade(self):
        # Nem mesmo uma categoria escolhida pode substituir a evidência de que o
        # código é aceito pelo produto.
        from apps.scrapers.ofertas import produtos_do_cupom
        Produto.objects.create(
            marketplace="mercadolivre", nome="Shampoo X", origem="oferta",
            preco_sem_desconto=50, preco_com_cupom=30, imagem_url="https://i/1.jpg",
            macro_categoria="Beleza e Cuidados Pessoais",
            link_produto="https://e.com/1")
        marca = CupomNormalizado.objects.create(
            fonte=self.cupom.fonte, external_id="campanha:777",
            marketplace="mercadolivre", titulo="25% OFF em Elseve", estado="ativo")

        self.assertEqual(produtos_do_cupom(marca), [])
        self.assertEqual(
            produtos_do_cupom(marca, macro="Beleza e Cuidados Pessoais"), [])


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

    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_ranking_ignores_active_product_older_than_catalog_ttl(self, get_marketplace):
        get_marketplace.return_value = Mock(is_alive=Mock(return_value=True))
        product = self._product("Oferta velha no ranking", 50)
        Produto.objects.filter(pk=product.pk).update(
            ultima_observacao=timezone.now() - timedelta(hours=49))

        from apps.scrapers.ofertas import selecionar_item_para_grupo
        selected = selecionar_item_para_grupo(
            usuario=self.user, grupo_id=self.group_a, min_desconto_percent=10)

        self.assertEqual(selected, [])

    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_ranking_collapses_duplicate_ml_variations_by_latest_observation(
        self, get_marketplace
    ):
        get_marketplace.return_value = Mock(is_alive=Mock(return_value=True))
        first = self._product("Top repetido", 30)
        second = self._product("Top repetido", 35)
        Produto.objects.filter(pk=first.pk).update(
            link_produto=("https://produto.mercadolivre.com.br/MLB-3102506128-item"
                          "?searchVariation=111"),
            ultima_observacao=timezone.now() - timedelta(hours=1),
        )
        Produto.objects.filter(pk=second.pk).update(
            link_produto=("https://produto.mercadolivre.com.br/MLB-3102506128-item"
                          "?searchVariation=222"),
        )
        first.refresh_from_db()
        second.refresh_from_db()

        from apps.scrapers.ofertas import selecionar_item_para_grupo
        selected = selecionar_item_para_grupo(
            usuario=self.user, grupo_id=self.group_a,
            min_desconto_percent=10, limite_envio=5)

        self.assertEqual(selected, [second])

    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_shortlist_does_not_repeat_live_validation(self, get_marketplace):
        product = self._product("Oferta para shortlist", 60)

        from apps.scrapers.ofertas import selecionar_item_para_grupo
        selected = selecionar_item_para_grupo(
            usuario=self.user, grupo_id=self.group_a,
            min_desconto_percent=10, verificar=False,
        )

        self.assertEqual(selected, [product])
        get_marketplace.assert_not_called()

    @patch("apps.scrapers.marketplaces.registry.get_marketplace")
    def test_ranking_loads_price_history_in_one_query(self, get_marketplace):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        products = [self._product(f"Produto {i}", 60) for i in range(20)]
        for product in products:
            registrar_preco("mercadolivre", "", product.link_produto, 80)
            registrar_preco("mercadolivre", "", product.link_produto, 60)

        from apps.scrapers.ofertas import selecionar_item_para_grupo
        with CaptureQueriesContext(connection) as queries:
            selected = selecionar_item_para_grupo(
                usuario=self.user, grupo_id=self.group_a,
                min_desconto_percent=10, limite_envio=20, verificar=False,
            )

        history_queries = [
            query for query in queries.captured_queries
            if "scrapers_precohistorico" in query["sql"].lower()
        ]
        self.assertEqual(len(selected), 20)
        # Uma janela de 30 dias decide elegibilidade e outra de 90 dias comprova
        # o preço riscado. O número é fixo, independentemente dos 20 produtos.
        self.assertEqual(len(history_queries), 2)

    @patch("django.db.connection")
    def test_postgres_price_history_is_aggregated_in_database(self, db_connection):
        product = self._product("Produto PostgreSQL", 60)
        db_connection.vendor = "postgresql"
        db_connection.ops.quote_name.return_value = '"scrapers_precohistorico"'
        cursor = db_connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [
            (f"mercadolivre:url:{product.link_produto}", 3, 60.0, 80.0),
        ]

        from apps.scrapers.precos import chave_produto, stats_em_lote
        result = stats_em_lote([product], dias=90)

        self.assertEqual(result[chave_produto(product)]["mediana"], 80.0)
        sql, params = cursor.execute.call_args.args
        self.assertIn("percentile_cont(0.5)", sql)
        self.assertIn("CROSS JOIN LATERAL", sql)
        self.assertIn(product.link_produto, str(params))

    def test_conversion_boost_uses_conservative_wilson_evidence(self):
        from apps.scrapers.content_ranking import _pontuar_conversao_loja

        self.assertLess(_pontuar_conversao_loja(1, 1), 2)
        self.assertGreater(_pontuar_conversao_loja(100, 10), 2)
        self.assertEqual(_pontuar_conversao_loja(100, 0), 0)

    def test_official_marketplace_conversion_is_applied_to_ranking(self):
        from apps.scrapers.content_ranking import (
            ContentCandidate, _aplicar_performance_marketplace,
        )

        product = self._product("Historico que converte", 60)
        ReceitaAfiliado.objects.create(
            usuario=self.user, marketplace="mercadolivre",
            data=timezone.localdate(), cliques=100, conversoes=10,
            pedidos=10, receita=1000, comissao=100,
            granularidade="dia", origem="auto", hash_origem="ranking-conversion",
        )
        candidate = ContentCandidate("product", product, 20, [])

        _aplicar_performance_marketplace(self.user, [candidate])

        self.assertGreater(candidate.score, 22)
        self.assertIn("boa conversão", candidate.reasons[0])

    def test_ab_variant_balances_only_messages_the_audience_may_have_seen(self):
        from apps.scrapers.ofertas import _variante_para_envio

        config = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id=self.group_a, variante_template="alternar",
        )
        for status, variante in (
            ("enviado", "A"), ("incerto", "A"),
            ("enviado", "B"), ("falhou", "B"), ("falhou", "B"),
        ):
            Publicacao.objects.create(
                usuario=self.user, configuracao=config, canal="whatsapp",
                destino_id=self.group_a, status=status, variante=variante,
            )

        self.assertEqual(_variante_para_envio(config), "B")

class MonitorCatalogMaintenanceTests(SimpleTestCase):
    @patch("apps.scrapers.maintenance.diagnosticar_alertas_pipeline_cupons",
           return_value={})
    @patch("apps.scrapers.maintenance.purgar_eventos_cupons_antigos",
           return_value=0)
    @patch("apps.scrapers.manual_scraping.atualizar_diagnostico_fila", return_value=0)
    @patch("apps.scrapers.manual_scraping.recuperar_jobs_abandonados", return_value=0)
    @patch("apps.scrapers.incidentes_saude.fechar_conexoes_restabelecidas",
           return_value=0)
    @patch("apps.scrapers.incidentes_saude.reconciliar_pendentes", return_value=0)
    @patch("apps.scrapers.maintenance.expire_stale",
           return_value={"products": 7, "coupons": 2})
    @patch("apps.scrapers.management.commands.monitorar.verificar_e_notificar",
           return_value={"checados": 1, "alertas_enviados": 0})
    def test_monitor_expires_catalog_even_without_scrape(
        self, _connections, expire, _reconcile, _close, _recover, _queue,
        _purge_coupon_events, _coupon_alerts,
    ):
        from apps.scrapers.management.commands.monitorar import Command

        result = Command()._ciclo()

        expire.assert_called_once_with()
        self.assertEqual(result["produtos_expirados"], 7)
        self.assertEqual(result["cupons_expirados"], 2)


class CouponCatalogFreshnessTests(TestCase):
    def setUp(self):
        self.public_source = FonteIngestao.objects.create(
            slug="coupon-freshness-source", marketplace="amazon", nome="Cupons")
        self.manual_source, _ = FonteIngestao.objects.get_or_create(
            slug="manual-private",
            defaults={"marketplace": "amazon", "nome": "Privados"},
        )

    def _coupon(self, source, external_id, **extra):
        values = {
            "fonte": source, "external_id": external_id,
            "marketplace": "amazon", "titulo": external_id,
            "codigo": "TESTE20", "estado": "ativo",
        }
        values.update(extra)
        return CupomNormalizado.objects.create(**values)

    def _age(self, coupon, hours=49):
        CupomNormalizado.objects.filter(pk=coupon.pk).update(
            ultima_observacao=timezone.now() - timedelta(hours=hours))

    def test_cleanup_expires_old_undated_coupon_but_preserves_manual_private(self):
        from apps.scrapers.maintenance import expire_stale

        public_old = self._coupon(self.public_source, "public-old")
        manual_old = self._coupon(self.manual_source, "manual-old")
        self._age(public_old)
        self._age(manual_old)

        result = expire_stale()

        public_old.refresh_from_db()
        manual_old.refresh_from_db()
        self.assertEqual(result["coupons"], 1)
        self.assertEqual(public_old.estado, "expirado")
        self.assertEqual(manual_old.estado, "ativo")

    def test_explicit_future_validity_wins_over_observation_age(self):
        from apps.scrapers.maintenance import cupons_frescos_q, expire_stale

        future = self._coupon(
            self.public_source, "future",
            validade=timezone.now() + timedelta(days=3),
        )
        self._age(future, hours=240)

        result = expire_stale()

        future.refresh_from_db()
        self.assertEqual(result["coupons"], 0)
        self.assertEqual(future.estado, "ativo")
        self.assertTrue(
            CupomNormalizado.objects.filter(pk=future.pk).filter(
                cupons_frescos_q()).exists())

    def test_cleanup_invalida_placeholder_legado_ativo(self):
        from apps.scrapers.maintenance import expire_stale
        from apps.scrapers.models import CupomFonteObservacao

        placeholder = self._coupon(
            self.public_source, "placeholder", codigo="MAISCUPONS",
        )
        observacao = CupomFonteObservacao.objects.create(
            fonte=self.public_source, cupom=placeholder,
            canonical_key="placeholder-key", source_external_id="placeholder",
            outcome="accepted",
        )

        result = expire_stale()

        placeholder.refresh_from_db()
        observacao.refresh_from_db()
        self.assertEqual(result["invalid_codes"], 1)
        self.assertEqual(placeholder.estado, "invalido")
        self.assertEqual(placeholder.confianca, "baixa")
        self.assertEqual(observacao.outcome, "invalid")
        self.assertEqual(observacao.reason_code, "invalid_coupon_code")


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

    def _patches(self, ofertas=10, cupons_codigo=3, cupons_campanha=5,
                 cupons_oficiais=4, campanha_erro=None):
        return (
            patch("apps.scrapers.scraper_mercadolivre.ofertas_scraper.mapear_ofertas",
                  return_value=ofertas),
            patch("apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper.mapear_cupons_codigo",
                  return_value=cupons_codigo),
            patch("apps.scrapers.scraper_mercadolivre.scraper.mapear_cupons",
                  side_effect=campanha_erro) if campanha_erro else
            patch("apps.scrapers.scraper_mercadolivre.scraper.mapear_cupons",
                  return_value=cupons_campanha),
            patch("apps.scrapers.sources.run_source",
                  return_value={"status": "ok", "offers": [], "coupons": []}),
            patch("apps.scrapers.sources.persistence.persist_items",
                  return_value={"offers": 0, "coupons": cupons_oficiais}),
            patch("apps.scrapers.coupon_products.preparar_lote",
                  return_value={"processados": cupons_oficiais,
                                "prontos": cupons_oficiais}),
        )

    def test_scrape_all_raspa_os_cupons_de_campanha(self):
        from apps.scrapers.models import ExecucaoIngestao

        p1, p2, p3, p4, p5, p6 = self._patches()
        with p1, p2, p3 as campanha, p4, p5, p6:
            get_marketplace("mercadolivre").scrape_all()

        campanha.assert_called_once()
        run = ExecucaoIngestao.objects.latest("id")
        # O scrape geral registra apenas a vitrine autenticada; códigos públicos,
        # preparo e links pertencem ao worker central de cupons.
        self.assertEqual(run.total_cupons, 3)
        self.assertEqual(run.status, "ok")

    def test_falha_nos_cupons_de_campanha_nao_derruba_ofertas(self):
        """O parser de campanha depende de um JSON embutido no bundle do ML — a peça
        mais frágil daqui. Se ele cair, ofertas e códigos ainda têm de entrar."""
        from apps.scrapers.models import ExecucaoIngestao

        p1, p2, p3, p4, p5, p6 = self._patches(
            campanha_erro=RuntimeError("NORDIC sumiu"))
        with p1, p2, p3, p4, p5, p6:
            get_marketplace("mercadolivre").scrape_all()

        run = ExecucaoIngestao.objects.latest("id")
        self.assertEqual(run.status, "ok")
        self.assertEqual(run.total_ofertas, 10)
        self.assertEqual(run.total_cupons, 3)
        self.assertTrue(EventoOperacional.objects.filter(
            evento="cupons_campanha_erro", level="warning").exists())

    def test_ofertas_sem_nenhum_cupom_vira_alerta(self):
        """800 ofertas e zero cupons era reportado como sucesso: o único sinal era o
        total zerado, e as ofertas sozinhas o mantinham positivo."""
        p1, p2, p3, p4, p5, p6 = self._patches(
            ofertas=800, cupons_codigo=0, cupons_campanha=0, cupons_oficiais=0)
        with p1, p2, p3, p4, p5, p6:
            get_marketplace("mercadolivre").scrape_all()

        evento = EventoOperacional.objects.get(evento="cupons_vazios")
        self.assertEqual(evento.level, "warning")
        self.assertEqual(evento.contexto["ofertas"], 800)

    def test_coleta_normal_nao_alerta(self):
        p1, p2, p3, p4, p5, p6 = self._patches()
        with p1, p2, p3, p4, p5, p6:
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
        self.assertEqual(cupom.estado, "inativo")
        self.assertEqual(Cupom.objects.filter(estado="ativo").count(), 3)
        self.assertEqual(cupom.tipo_desconto, "fixo")
        self.assertEqual(cupom.valor_desconto, 50.0)
        self.assertEqual(cupom.valor_minimo, 399.0)
        self.assertEqual(cupom.desconto_maximo, 50.0)
        self.assertIsNotNone(cupom.validade)

    def test_parser_preserva_milhar_centavos_e_teto(self):
        from apps.scrapers.scraper_mercadolivre.scraper import mapear_cupons

        with open(self.DUMP, encoding="utf-8") as f:
            dados = json.load(f)
        vazio = {"appProps": {"pageProps": {"filteredCouponsData": {"coupons": []}}}}
        sess = self._http_falsa([dados, vazio])
        with patch(
            "apps.scrapers.scraper_mercadolivre.scraper._ml_http_session",
            return_value=sess,
        ):
            mapear_cupons()

        apple = Cupom.objects.get(titulo__icontains="em Apple")
        self.assertEqual(apple.valor_minimo, 2000.0)
        self.assertEqual(apple.desconto_maximo, 250.0)

        smart = Cupom.objects.get(titulo__icontains="Smart Home")
        self.assertEqual(smart.valor_minimo, 99.90)
        self.assertEqual(smart.desconto_maximo, 42.0)
        self.assertEqual(smart.tipo_desconto, "porcentagem")
        self.assertEqual(smart.valor_desconto, 40.0)

    def test_helpers_do_payload_nao_confundem_urgencia_com_validade(self):
        from apps.scrapers.scraper_mercadolivre.scraper import _validade_ml, _valor_ml

        agora = timezone.now().replace(month=5, day=1)
        vence = _validade_ml("Vence 19 de maio", agora=agora)
        self.assertEqual((vence.month, vence.day, vence.hour), (5, 19, 23))
        for texto in (
            "Está esgotando!", "Termina em 3 horas!", "Vence em domingo", "", None,
        ):
            self.assertIsNone(_validade_ml(texto, agora=agora))

        self.assertEqual(_valor_ml("2.000", "0"), 2000.0)
        self.assertEqual(_valor_ml("99", "9"), 99.90)
        self.assertIsNone(_valor_ml(None))

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

    def test_pagina_repetida_para_sem_expirar_catalogo_anterior(self):
        """Se `?page=` for ignorado, a segunda página não pode rodar até 200."""
        from apps.scrapers.scraper_mercadolivre.scraper import mapear_cupons

        Cupom.objects.create(campanha_id="catalogo-anterior", titulo="Preservado",
                             estado="ativo", valor_desconto=10.0, valor_minimo=0.0)
        payload = {"appProps": {"pageProps": {"filteredCouponsData": {
            "coupons": [{"campaignId": "repetido-1",
                         "title": {"text": "Cupom repetido"},
                         "action": {"type": "button"}}]}}}}
        sess = self._http_falsa([payload, payload, payload])

        with patch("apps.scrapers.scraper_mercadolivre.scraper._ml_http_session",
                   return_value=sess):
            salvos = mapear_cupons()

        self.assertEqual(salvos, 1)
        self.assertLessEqual(sess.get.call_count, 2)
        self.assertEqual(Cupom.objects.get(campanha_id="catalogo-anterior").estado,
                         "ativo")

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


class CredencialDaRaspagemTests(TestCase):
    """A raspagem tem de usar a sessão ML CIFRADA, não um arquivo que não existe.

    Regressão do bug "ML conectado na tela, mas a raspagem não traz nada": os
    scrapers chamavam `ml_auth_path()` sem usuário, que devolve "" desde a
    migração multi-tenant. O `open("")` falhava calado, o cookie jar saía vazio e
    o GET em /cupons/filter voltava sem payload — indistinguível de "não há
    cupons". O portão da tela, que sonda a sessão do banco, deixava passar.
    """

    def setUp(self):
        from apps.accounts.models import ensure_personal_organization

        self.user = get_user_model().objects.create_user("dono-cupom", password="x")
        self.organization = ensure_personal_organization(self.user)
        self.state = {"cookies": [{"name": "ssid", "value": "segredo",
                                   "domain": ".mercadolivre.com.br", "path": "/"}],
                      "origins": []}

    def test_sessao_do_usuario_vira_cookie_no_get(self):
        from apps.accounts.ml_sessions import save_storage_state
        from apps.scrapers.scraper_mercadolivre.scraper import mapear_cupons

        save_storage_state(self.user, self.state)
        vazio = {"appProps": {"pageProps": {"filteredCouponsData": {"coupons": []}}}}
        capturadas = []

        def _sessao_falsa(state):
            capturadas.append(state)
            sess = Mock()
            sess.get.return_value = Mock(
                text=f"<script>{json.dumps(vazio)}</script>",
                raise_for_status=Mock(),
            )
            return sess

        with patch("apps.scrapers.scraper_mercadolivre.scraper._ml_http_session",
                   side_effect=_sessao_falsa):
            mapear_cupons(usuario=self.user)

        self.assertEqual(len(capturadas), 1)
        self.assertEqual([c["name"] for c in capturadas[0]["cookies"]], ["ssid"])

    def test_sem_usuario_cai_na_organizacao_de_sistema(self):
        """O loop automático (@system_job) não tem usuário: usa a org designada."""
        from apps.accounts.ml_sessions import save_storage_state
        from apps.scrapers.ml_auth import storage_state

        save_storage_state(self.user, self.state)
        with self.settings(ML_SYSTEM_ORGANIZATION_ID=str(self.organization.pk)):
            self.assertEqual(storage_state(None), self.state)

    def test_sem_organizacao_de_sistema_devolve_none(self):
        from apps.scrapers.ml_auth import storage_state

        with self.settings(ML_SYSTEM_ORGANIZATION_ID=""):
            self.assertIsNone(storage_state(None))

    def test_http_session_monta_o_jar_a_partir_do_dict(self):
        """Antes recebia um caminho de arquivo; agora, o storage_state já resolvido."""
        from apps.scrapers.scraper_mercadolivre.scraper import _ml_http_session

        sess = _ml_http_session(self.state)
        self.assertEqual(sess.cookies.get("ssid", domain=".mercadolivre.com.br"),
                         "segredo")
        # None é entrada válida (não há sessão): jar vazio, sem estourar.
        self.assertEqual(len(_ml_http_session(None).cookies), 0)


class LoginMLDesativadoTests(TestCase):
    """Com a flag off, a tela precisa DIZER isso.

    Regressão do "Reconectar não faz nada": `criar_sessao` devolvia
    fase='indisponivel' sem gravar no cache, então o poll de 5s relia 'idle' +
    auth_valido=True (a sessão antiga seguia no banco) e repintava "Conectado" —
    a sessão parecia presa.
    """

    def setUp(self):
        from apps.accounts.models import ensure_personal_organization

        cache.clear()
        self.user = get_user_model().objects.create_user("sem-login-ml", password="x")
        self.user.perfil.marcar_verificado()
        ensure_personal_organization(self.user)
        self.url = reverse("scraper-ml-desconectar")

    def test_recusa_fica_no_cache_e_o_status_seguinte_nao_diz_conectado(self):
        from apps.scrapers import ml_conexao

        with self.settings(ML_BROWSER_LOGIN_ENABLED=False):
            recusa = ml_conexao.criar_sessao(self.user)
            self.assertEqual(recusa["fase"], "indisponivel")
            self.assertTrue(recusa["erro"])
            # O poll seguinte NÃO pode voltar para 'idle' (que a tela converte em
            # "Conectado" quando há sessão salva).
            self.assertEqual(ml_conexao.status(self.user.id)["fase"], "indisponivel")

    def test_desconectar_apaga_a_sessao_e_limpa_o_estado(self):
        from apps.accounts.ml_sessions import has_storage_state, save_storage_state
        from apps.scrapers import ml_conexao

        save_storage_state(self.user, {"cookies": [], "origins": []})
        ml_conexao._set_estado(self.user.id, fase="conectado")
        self.client.force_login(self.user)

        resposta = self.client.post(self.url)

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json()["apagou"])
        self.assertFalse(has_storage_state(self.user))
        self.assertEqual(ml_conexao.status(self.user.id)["fase"], "idle")

    def test_desconectar_exige_post(self):
        # Apagar credencial é efeito colateral: GET deixaria a rota sem CSRF.
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_desconectar_exige_login(self):
        self.client.logout()
        self.assertIn(self.client.post(self.url).status_code, (302, 403))


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
            slug="mercadolivre-campanhas", defaults={
                "marketplace": "mercadolivre",
                "nome": "Mercado Livre — campanhas autenticadas"})
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

    def test_projeta_catalogo_grande_em_lotes_com_observacoes_e_chaves(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        from apps.scrapers.models import CupomFonteObservacao
        from apps.scrapers.scraper_mercadolivre.scraper import projetar_catalogo_cupons

        Cupom.objects.bulk_create([
            Cupom(
                campanha_id=f"LOTE-{i}", titulo=f"Cupom {i}", estado="ativo",
                tipo_desconto="percentual", valor_desconto=10,
                ultima_verificacao=timezone.now(),
            )
            for i in range(120)
        ])

        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(projetar_catalogo_cupons(), 120)

        self.assertLess(len(queries), 35)
        projetados = CupomNormalizado.objects.filter(
            external_id__startswith="campanha:LOTE-",
        )
        self.assertEqual(projetados.count(), 120)
        self.assertFalse(projetados.filter(produtos_chave="").exists())
        self.assertEqual(CupomFonteObservacao.objects.filter(
            source_external_id__startswith="campanha:LOTE-",
        ).count(), 120)


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
            verificado_ok=True, url_canonica="https://mercadolivre.com/sec/meu-link",
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("scraper-top"), {"loja": "mercadolivre"})

        listados = {p.id: p for p in response.context["produtos"]}
        self.assertTrue(listados[produto.id].afiliado_pronto)

    def _catalogo_com_afiliacao_pronta_e_com_erro(self):
        pronto = self._produto_ml("Fone pronto")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=pronto, estado="pronto",
            link_afiliado="https://meli.la/pronto", afiliado_ok=True,
            verificado_ok=True, url_canonica="https://meli.la/pronto")
        falhou = self._produto_ml("Fone falhou")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=falhou, estado="erro",
            ultimo_erro="O Link Builder recusou a URL.",
            ultima_tentativa=timezone.now())

    def test_tela_promocoes_mostra_resumo_e_ultimo_erro_da_afiliacao_ao_admin(self):
        self._catalogo_com_afiliacao_pronta_e_com_erro()
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("scraper-top"), {"loja": "mercadolivre"})

        self.assertEqual(response.context["afiliacao"]["prontos"], 1)
        self.assertEqual(response.context["afiliacao"]["erro"], 1)
        self.assertContains(response, "Afiliação: 1 prontos")
        self.assertContains(response, "1 com erro")
        self.assertContains(response, "O Link Builder recusou a URL.")

    def test_usuario_comum_nao_ve_a_faixa_de_diagnostico_nem_o_erro_cru(self):
        """Fila de afiliação e mensagem do Link Builder são leitura de operação.

        Quem usa a tela não religa fonte nem reconecta sessão compartilhada — o
        painel só lhe dava um texto técnico sem ação possível. E o resumo custava
        três agregações sobre o catálogo inteiro em todo GET, então escondê-lo é
        também o que tira esse peso da request do usuário comum.
        """
        self._catalogo_com_afiliacao_pronta_e_com_erro()
        self.assertFalse(self.user.is_staff)
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("scraper-top"), {"loja": "mercadolivre"})

        self.assertIsNone(response.context["afiliacao"])
        self.assertNotContains(response, "Afiliação:")
        self.assertNotContains(response, "O Link Builder recusou a URL.")
        self.assertNotContains(response, "Saúde das fontes")
        # A lista em si continua inteira: escondemos o diagnóstico, não as ofertas.
        self.assertEqual(response.context["page_obj"].paginator.count, 1)

    def test_tela_promocoes_resolve_afiliacao_em_lote(self):
        # preparar_exibicao existe pra isto: uma query por página, não por produto.
        # Sem o lote, corrigir o badge trocaria o bug por 20 queries por load.
        for i in range(5):
            LinkAfiliadoUsuario.objects.create(
                usuario=self.user, produto=self._produto_ml(f"Fone {i}"),
                link_afiliado=f"https://mercadolivre.com/sec/l{i}", afiliado_ok=True,
                verificado_ok=True, url_canonica=f"https://mercadolivre.com/sec/l{i}",
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

    def test_paginacao_inclui_afiliados_fora_dos_200_maiores_descontos(self):
        # Regressão: a view cortava os 200 maiores descontos antes de filtrar quem
        # tinha link. Um afiliado com desconto menor nunca aparecia em página alguma.
        Produto.objects.bulk_create([
            Produto(
                marketplace="mercadolivre", nome=f"Sem link {i}", origem="oferta",
                preco_sem_desconto=100, preco_com_cupom=1,
                link_produto=f"https://example.com/sem-link-{i}",
            )
            for i in range(205)
        ])
        pronto = Produto.objects.create(
            marketplace="mercadolivre", nome="Afiliado fora do corte", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=90,
            link_produto="https://example.com/afiliado-fora-do-corte",
        )
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=pronto, estado="pronto",
            link_afiliado="https://meli.la/fora-do-corte", afiliado_ok=True,
            verificado_ok=True, url_canonica="https://meli.la/fora-do-corte",
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("scraper-top"), {"loja": "mercadolivre"})

        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertEqual([p.id for p in response.context["produtos"]], [pronto.id])

    def test_tela_promocoes_marca_item_amazon_como_pronto_sem_gravar_no_banco(self):
        produto = self._produto_amazon()
        self.client.force_login(self.user)

        response = self.client.get(reverse("scraper-top"), {"loja": "amazon"})

        listados = {p.id: p for p in response.context["produtos"]}
        self.assertTrue(listados[produto.id].afiliado_pronto)
        # A visita é um GET: nada de escrita no campo persistido.
        produto.refresh_from_db()
        self.assertFalse(produto.afiliado_ok)


class LinkValidacaoSoTTests(SimpleTestCase):
    """Fonte única de normalização + decisão de aprovação (link_validacao).

    Uma decisão só: a MESMA que a listagem, a geração e o envio aplicam. Cobre a
    regressão do produto que caía na vitrine /social/ do afiliado.
    """

    def test_normaliza_barra_final_preservando_parametros(self):
        from apps.scrapers.link_validacao import normalizar_url
        self.assertEqual(normalizar_url("https://meli.la/abc/"), "https://meli.la/abc")
        # Query string (parâmetros de afiliação) é preservada intacta.
        u = "https://produto.mercadolivre.com.br/MLB-123?matt_tool=1&x=2"
        self.assertEqual(normalizar_url(u), u)
        self.assertEqual(normalizar_url("  https://meli.la/x  "), "https://meli.la/x")

    def test_classifica_pagina_de_produto_e_vitrine_social(self):
        from apps.scrapers.link_validacao import eh_pagina_produto, eh_vitrine_social
        self.assertTrue(eh_pagina_produto("https://produto.mercadolivre.com.br/MLB-123456"))
        self.assertTrue(eh_pagina_produto("https://www.mercadolivre.com.br/p/MLB123"))
        self.assertFalse(eh_pagina_produto(
            "https://www.mercadolivre.com.br/social/loja/lists"))
        self.assertTrue(eh_vitrine_social(
            "https://www.mercadolivre.com.br/social/loja/lists"))

    def test_oferta_na_vitrine_social_sem_nome_e_reprovada(self):
        # Regressão do caso reportado: meli.la -> /social/.../lists. is_landing=True,
        # mas o nome do produto não aparece (nome_confere=False) -> NÃO aprovado.
        from apps.scrapers.link_validacao import aprovado_por_relatorio, motivo_reprovacao
        relatorio = {
            "url_final": "https://www.mercadolivre.com.br/social/loja/lists",
            "is_pagina_produto": False, "is_landing_afiliado": True,
            "nome_confere": False, "preco_visivel": None,
            "cupom_detectado": False, "preco_riscado": False, "erros": [],
        }
        self.assertFalse(aprovado_por_relatorio(relatorio, confiar_desconto=True))
        self.assertIn("vitrine", motivo_reprovacao(relatorio, True).lower())

    def test_oferta_na_pagina_do_produto_com_nome_e_aprovada(self):
        from apps.scrapers.link_validacao import aprovado_por_relatorio
        relatorio = {
            "url_final": "https://produto.mercadolivre.com.br/MLB-123456",
            "is_pagina_produto": True, "is_landing_afiliado": False,
            "nome_confere": True, "preco_visivel": "R$ 100",
            "cupom_detectado": False, "preco_riscado": False, "erros": [],
        }
        self.assertTrue(aprovado_por_relatorio(relatorio, confiar_desconto=True))

    def test_cupom_exige_desconto_confirmado_na_pagina_do_produto(self):
        from apps.scrapers.link_validacao import aprovado_por_relatorio
        base = {
            "url_final": "https://produto.mercadolivre.com.br/MLB-123456",
            "is_pagina_produto": True, "is_landing_afiliado": False,
            "nome_confere": True, "preco_visivel": "R$ 100", "erros": [],
        }
        sem_desconto = {**base, "cupom_detectado": False, "preco_riscado": False}
        com_desconto = {**base, "cupom_detectado": True, "preco_riscado": False}
        self.assertFalse(aprovado_por_relatorio(sem_desconto, confiar_desconto=False))
        self.assertTrue(aprovado_por_relatorio(com_desconto, confiar_desconto=False))


class EnviabilidadeConsistenteTests(TestCase):
    """Invariante central: item exibido como enviável ⇔ link já verificado, e o
    envio usa exatamente a URL canônica aprovada, sem reprová-la de novo."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("consistencia", password="test")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)

    def _produto(self, nome="Máquina de Solda Inversora 250a"):
        return Produto.objects.create(
            marketplace="mercadolivre", nome=nome, origem="oferta",
            preco_sem_desconto=300, preco_com_cupom=200,
            link_produto="https://www.mercadolivre.com.br/maquina-solda-250a",
        )

    def _link(self, produto, **extra):
        defaults = dict(usuario=self.user, produto=produto, afiliado_ok=True,
                        estado="pronto", link_afiliado="https://meli.la/canonica",
                        url_isca=produto.link_produto)
        defaults.update(extra)
        return LinkAfiliadoUsuario.objects.create(**defaults)

    def test_link_verificado_aparece_como_enviavel(self):
        produto = self._produto("Fone verificado")
        self._link(produto, verificado_ok=True, url_canonica="https://meli.la/canonica")

        response = self.client.get(reverse("scraper-top"), {"loja": "mercadolivre"})
        listados = {p.id: p for p in response.context["produtos"]}
        self.assertIn(produto.id, listados)
        self.assertTrue(listados[produto.id].afiliado_pronto)

    def test_link_reprovado_nao_chega_a_tela_de_envio(self):
        # verificado_ok=False: o link existe mas não abre o anúncio certo. NÃO pode
        # ser enviável e não aparece na lista padrão (só afiliados prontos).
        produto = self._produto("Solda vitrine social")
        self._link(produto, verificado_ok=False,
                   verificacao_motivo="O link abre a vitrine do afiliado, não o anúncio.")

        response = self.client.get(reverse("scraper-top"), {"loja": "mercadolivre"})
        self.assertNotIn(
            produto.id, {p.id for p in response.context["produtos"]})

        # Na visão "todos", aparece com status link_invalido e motivo — sem botão.
        response = self.client.get(
            reverse("scraper-top"), {"loja": "mercadolivre", "afiliado": "todos"})
        listados = {p.id: p for p in response.context["produtos"]}
        self.assertIn(produto.id, listados)
        self.assertFalse(listados[produto.id].afiliado_pronto)
        self.assertEqual(listados[produto.id].afiliado_estado, "link_invalido")
        self.assertIn("vitrine", listados[produto.id].afiliado_motivo.lower())

    def test_link_sem_veredito_fica_verificando_nao_enviavel(self):
        produto = self._produto("Aguardando verificação")
        self._link(produto, verificado_ok=None)

        response = self.client.get(
            reverse("scraper-top"), {"loja": "mercadolivre", "afiliado": "todos"})
        listados = {p.id: p for p in response.context["produtos"]}
        self.assertFalse(listados[produto.id].afiliado_pronto)
        self.assertEqual(listados[produto.id].afiliado_estado, "verificando")

    @patch("apps.scrapers.ofertas._baixar_imagem_b64", return_value=(None, None))
    @patch("apps.scrapers.senders.whatsapp.WhatsAppSender.enviar_oferta")
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.verify_link")
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.build_affiliate_link")
    def test_envio_confia_no_veredito_e_usa_url_canonica_sem_reverificar(
        self, build_link, verify_link, send, _img
    ):
        from apps.scrapers.ofertas import enviar_oferta_de_produto
        produto = self._produto()
        # Link já APROVADO: o build devolve o veredito + a URL canônica aprovada.
        build_link.return_value = {
            "link_afiliado": "https://meli.la/canonica", "afiliado_ok": True,
            "verificado_ok": True, "url_canonica": "https://meli.la/canonica",
        }
        send.return_value = {"sucesso": True, "via": "whatsapp-test"}

        result = enviar_oferta_de_produto(
            produto, "grupo@g.us", usuario=self.user, destino_nome="Teste ofertas")

        self.assertTrue(result["sucesso"])
        # Não reverifica ao vivo (sem segunda implementação divergente)...
        verify_link.assert_not_called()
        # ...e envia EXATAMENTE a URL canônica aprovada.
        self.assertEqual(result["link"], "https://meli.la/canonica")

    @patch("apps.scrapers.ofertas._baixar_imagem_b64", return_value=(None, None))
    @patch("apps.scrapers.senders.whatsapp.WhatsAppSender.enviar_oferta")
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.verify_link")
    @patch("apps.scrapers.marketplaces.mercadolivre.MercadoLivre.build_affiliate_link")
    def test_envio_de_link_nao_verificado_reprovado_persiste_e_some_da_tela(
        self, build_link, verify_link, send, _img
    ):
        from apps.scrapers.ofertas import enviar_oferta_de_produto
        produto = self._produto("Solda sem veredito")
        linha = self._link(produto, verificado_ok=None)
        # build sem veredito (ex.: envio automático); a verificação ao vivo reprova.
        build_link.return_value = {
            "link_afiliado": "https://meli.la/canonica", "afiliado_ok": True,
            "verificado_ok": None, "url_canonica": "https://meli.la/canonica",
        }
        verify_link.return_value = {
            "ok": False,
            "url_final": "https://www.mercadolivre.com.br/social/loja/lists",
            "is_pagina_produto": False, "is_landing_afiliado": True,
            "nome_confere": False, "preco_visivel": None, "erros": [],
        }

        result = enviar_oferta_de_produto(
            produto, "grupo@g.us", usuario=self.user, destino_nome="Teste ofertas")

        self.assertFalse(result["sucesso"])
        self.assertEqual(result["motivo"], "link reprovado na verificação")
        send.assert_not_called()
        # Self-heal: o veredito é persistido; da próxima vez o item já não é enviável.
        linha.refresh_from_db()
        self.assertIs(linha.verificado_ok, False)
        self.assertTrue(linha.verificacao_motivo)


class VerificarLinksPendentesTests(TestCase):
    """A passada que APROVA o destino antes de o item virar enviável."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("aprovador", password="test")

    def _produto(self, nome):
        return Produto.objects.create(
            marketplace="mercadolivre", nome=nome, origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=60,
            link_produto="https://www.mercadolivre.com.br/item",
        )

    def setUpBrowserFalso(self):
        """O lote agora reusa UM browser: fingimos o `iniciar_browser` dele.

        Os testes fazem patch de `_relatorio_na_pagina` (o trabalho por link) em vez
        de `verificar_link_afiliado` (o caminho de item único, que abre browser
        próprio) — é essa a função que o lote passou a usar.
        """
        from contextlib import contextmanager

        @contextmanager
        def _browser_falso(*a, **kw):
            yield MagicMock(), MagicMock()

        remendo = patch("apps.scrapers.scraper_mercadolivre.link.iniciar_browser",
                        _browser_falso)
        remendo.start()
        self.addCleanup(remendo.stop)

    @patch("apps.scrapers.scraper_mercadolivre.link._relatorio_na_pagina")
    def test_aprova_link_que_abre_o_produto(self, verify):
        self.setUpBrowserFalso()
        produto = self._produto("Fone bom")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto, afiliado_ok=True, estado="pronto",
            link_afiliado="https://meli.la/bom", verificado_ok=None)
        verify.return_value = {"ok": True, "url_final": "https://produto.mercadolivre.com.br/MLB-1"}

        r = ml_link.verificar_links_pendentes(self.user, limite=10)

        self.assertEqual(r["aprovados"], 1)
        linha = LinkAfiliadoUsuario.objects.get(usuario=self.user, produto=produto)
        self.assertIs(linha.verificado_ok, True)
        self.assertEqual(linha.url_canonica, "https://meli.la/bom")

    @patch("apps.scrapers.scraper_mercadolivre.link._relatorio_na_pagina")
    @patch("apps.scrapers.scraper_mercadolivre.link.interesse_pendente",
           return_value=True)
    def test_cede_browser_entre_destinos_quando_fonte_aguarda(self, pending, verify):
        self.setUpBrowserFalso()
        primeiro = self._produto("Primeiro destino")
        segundo = self._produto("Segundo destino")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=primeiro, afiliado_ok=True, estado="pronto",
            link_afiliado="https://meli.la/primeiro", verificado_ok=None,
        )
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=segundo, afiliado_ok=True, estado="pronto",
            link_afiliado="https://meli.la/segundo", verificado_ok=None,
        )
        verify.return_value = {
            "ok": True, "url_final": "https://produto.mercadolivre.com.br/MLB-1",
        }

        resultado = ml_link.verificar_links_pendentes(self.user, limite=10)

        self.assertEqual(resultado["aprovados"], 1)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(
            LinkAfiliadoUsuario.objects.filter(verificado_ok__isnull=True).count(), 1,
        )
        pending.assert_called_once_with(
            "django_chromium", exceto="links_verify",
        )

    @patch("apps.scrapers.scraper_mercadolivre.link._relatorio_na_pagina")
    def test_aprova_grava_matt_word_da_url_final(self, verify):
        self.setUpBrowserFalso()
        produto = self._produto("Fone com rastreio")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto, afiliado_ok=True, estado="pronto",
            link_afiliado="https://meli.la/rast", verificado_ok=None)
        verify.return_value = {
            "ok": True,
            "url_final": (
                "https://www.mercadolivre.com.br/social/lules"
                "?matt_word=lules&matt_tool=android&ref=" + ("x" * 2000)
            ),
        }

        r = ml_link.verificar_links_pendentes(self.user, limite=10)

        self.assertEqual(r["aprovados"], 1)
        linha = LinkAfiliadoUsuario.objects.get(usuario=self.user, produto=produto)
        self.assertIs(linha.verificado_ok, True)
        self.assertIn("matt_word=lules", linha.url_canonica)
        self.assertNotIn("ref=", linha.url_canonica)
        self.assertLessEqual(len(linha.url_canonica), 1000)

    @patch("apps.scrapers.scraper_mercadolivre.link._relatorio_na_pagina")
    def test_reprova_link_que_cai_na_vitrine_social(self, verify):
        self.setUpBrowserFalso()
        produto = self._produto("Solda vitrine")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto, afiliado_ok=True, estado="pronto",
            link_afiliado="https://meli.la/ruim", verificado_ok=None)
        verify.return_value = {
            "ok": False, "url_final": "https://www.mercadolivre.com.br/social/loja/lists",
            "is_pagina_produto": False, "is_landing_afiliado": True,
            "nome_confere": False, "preco_visivel": None, "erros": [],
        }

        r = ml_link.verificar_links_pendentes(self.user, limite=10)

        self.assertEqual(r["reprovados"], 1)
        linha = LinkAfiliadoUsuario.objects.get(usuario=self.user, produto=produto)
        self.assertIs(linha.verificado_ok, False)
        self.assertTrue(linha.verificacao_motivo)
        self.assertGreater(linha.proxima_tentativa, timezone.now())
        self.assertEqual(linha.estado, "pronto")

    @patch("apps.scrapers.scraper_mercadolivre.link._relatorio_na_pagina")
    def test_link_reprovado_nao_e_reverificado_com_a_mesma_url(self, verify):
        """Reprovado pertence à fila de GERAÇÃO, não à de verificação.

        Reabrir a mesma URL reprovada a cada backoff era o ciclo que prendia ~973
        links ML em produção: a geração pulava quem já tinha URL e a verificação só
        reconfirmava a reprovação. O item só volta a valer um Chromium depois de ter
        a URL substituída — ver ids_com_link_utilizavel.
        """
        self.setUpBrowserFalso()
        produto = self._produto("Link para regerar")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto, afiliado_ok=True, estado="pronto",
            link_afiliado="https://meli.la/retry", verificado_ok=False,
            proxima_tentativa=timezone.now() - timezone.timedelta(minutes=1),
        )

        resultado = ml_link.verificar_links_pendentes(self.user, limite=10)

        self.assertEqual(resultado,
                         {"aprovados": 0, "reprovados": 0, "transitorios": 0})
        verify.assert_not_called()

    def test_link_reprovado_com_backoff_vencido_volta_a_geracao(self):
        from apps.scrapers.afiliado import ids_com_link_utilizavel

        produto = self._produto("Link para regerar")
        linha = LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto, afiliado_ok=True, estado="pronto",
            link_afiliado="https://meli.la/retry", verificado_ok=False,
            proxima_tentativa=timezone.now() - timezone.timedelta(minutes=1),
        )
        self.assertEqual(
            ids_com_link_utilizavel(self.user, [produto]), set(),
            "link reprovado com backoff vencido tem de voltar para a geração")

        # Ainda dentro do backoff: fica fora da fila.
        LinkAfiliadoUsuario.objects.filter(pk=linha.pk).update(
            proxima_tentativa=timezone.now() + timezone.timedelta(minutes=30))
        self.assertEqual(
            ids_com_link_utilizavel(self.user, [produto]), {produto.id})

        # Aprovado ou aguardando veredito: também fora da fila de geração.
        for veredito in (True, None):
            LinkAfiliadoUsuario.objects.filter(pk=linha.pk).update(
                verificado_ok=veredito, proxima_tentativa=None)
            self.assertEqual(
                ids_com_link_utilizavel(self.user, [produto]), {produto.id})

    def test_regeracao_substitui_a_url_reprovada(self):
        """A geração troca a URL e devolve o item ao estado 'sem veredito'."""
        produto = self._produto("Link regerado")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto, afiliado_ok=True, estado="pronto",
            link_afiliado="https://meli.la/reprovado", verificado_ok=False,
            verificacao_motivo="O link abre a vitrine do afiliado.",
            tentativas=2,
            proxima_tentativa=timezone.now() - timezone.timedelta(minutes=1),
        )
        self.setUpBrowserFalso()
        with organization_context(self.user.personal_organization), \
                patch("apps.scrapers.scraper_mercadolivre.link.has_storage_state",
                      return_value=True), \
                patch("apps.scrapers.scraper_mercadolivre.link._abrir_link_builder"), \
                patch("apps.scrapers.scraper_mercadolivre.link._afiliar_url_na_pagina",
                      return_value="https://meli.la/novo"):
            gerados, falhas = ml_link.gerar_links_em_lote([produto], usuario=self.user)

        self.assertEqual((gerados, falhas), (1, 0))
        linha = LinkAfiliadoUsuario.objects.get(usuario=self.user, produto=produto)
        self.assertEqual(linha.link_afiliado, "https://meli.la/novo")
        self.assertIsNone(linha.verificado_ok)
        self.assertEqual(linha.verificacao_motivo, "")
        self.assertIsNone(linha.proxima_tentativa)

    def test_link_reprovado_esgotado_vira_nao_afiliavel(self):
        from apps.scrapers.afiliado import (
            MAX_TENTATIVAS_ERRO, registrar_reprovacao,
        )

        produto = self._produto("Link definitivamente inválido")
        linha = LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto, afiliado_ok=True, estado="pronto",
            link_afiliado="https://meli.la/terminal", verificado_ok=None,
        )
        for _ in range(MAX_TENTATIVAS_ERRO):
            registrar_reprovacao(self.user, produto, "Destino não aprovado")

        linha.refresh_from_db()
        self.assertEqual(linha.estado, "nao_afiliavel")
        self.assertIsNone(linha.proxima_tentativa)
        self.assertEqual(linha.tentativas, MAX_TENTATIVAS_ERRO)

    @patch("apps.scrapers.scraper_mercadolivre.link._relatorio_na_pagina")
    def test_falha_de_rede_e_transitoria_nao_reprova(self, verify):
        self.setUpBrowserFalso()
        produto = self._produto("Fone rede caiu")
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto, afiliado_ok=True, estado="pronto",
            link_afiliado="https://meli.la/rede", verificado_ok=None)
        verify.return_value = {"ok": False, "erros": ["Falha ao abrir link: timeout"]}

        r = ml_link.verificar_links_pendentes(self.user, limite=10)

        self.assertEqual(r["transitorios"], 1)
        linha = LinkAfiliadoUsuario.objects.get(usuario=self.user, produto=produto)
        # Segue None: nem aprovado nem reprovado — será retentado, não some da fila.
        self.assertIsNone(linha.verificado_ok)
        self.assertGreater(linha.proxima_tentativa, timezone.now())


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

    def test_vazio_e_invalido_nao_sao_confundidos_com_zero(self):
        from apps.scrapers.relatorios import ReportCellInvalid, _num, _num_typed

        for vazio in ("", None, "—", "-"):
            self.assertEqual(_num_typed(vazio), ("empty", None), vazio)
        with self.assertRaises(ReportCellInvalid):
            _num("n/d")
        self.assertEqual(_num("0"), 0.0)


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
        linhas = self._extrair([[
            "grupo-casa", "Fone JBL", "1.234", "12", "12",
            "R$ 1.999,90", "R$ 199,99",
        ]])

        self.assertEqual(len(linhas), 1)
        self.assertEqual(linhas[0].cliques, 1234)
        self.assertEqual(linhas[0].conversoes, 12)
        self.assertEqual(linhas[0].pedidos, 12)
        self.assertEqual(linhas[0].receita, 1999.90)
        self.assertEqual(linhas[0].comissao, 199.99)

    def test_tabela_sem_numero_reconhecido_falha_em_vez_de_reportar_zero(self):
        from apps.scrapers.relatorios import ReportSyncError

        # Achar a tabela e não entender número nenhum é parser errado, não conta
        # zerada. Reportar "ok" aqui é o que produzia R$ 0,00 com selo verde.
        with self.assertRaises(ReportSyncError):
            self._extrair([[
                "grupo", "Fone", "n/d", "n/d", "n/d", "n/d", "n/d",
            ]])

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
            link_produto="https://produto.mercadolivre.com.br/MLB-123456789", **extra)

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
        self.assertEqual(res["gerados"], 1)
        self.assertEqual(res["falhas"], 0)
        self.assertEqual(res["pulados"], 0)
        self.assertEqual(res["por_marketplace"], {"mercadolivre": {"gerados": 1,
                                                                  "falhas": 0}})

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

        resultado = _rodar_links(lote=40)
        self.assertEqual(
            {chave: resultado[chave] for chave in ("gerados", "falhas", "pulados")},
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

    def test_lote_grava_o_link_sem_nenhum_bypass_de_async(self):
        """O lote persiste item a item sem tocar em DJANGO_ALLOW_ASYNC_UNSAFE.

        Antes, o lote inteiro rodava dentro de um contextmanager que setava essa
        variável no os.environ do PROCESSO. Como ela é global às 8 threads do
        gunicorn, o `finally` de um fluxo a removia no meio de outro — a origem do
        "às vezes funciona, às vezes não". Agora cada gravação vai por
        executar_no_tenant, e o ambiente não é tocado em momento nenhum.
        """
        produto = self._produto()
        from apps.accounts.ml_sessions import save_storage_state
        save_storage_state(
            self.user,
            {"cookies": [{"name": "ssid", "value": "x"}], "origins": []},
        )
        anterior = os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)

        @contextmanager
        def browser_falso(**_kwargs):
            yield Mock(), Mock()

        try:
            with organization_context(self.user.personal_organization), \
                 patch("apps.scrapers.scraper_mercadolivre.link.iniciar_browser", browser_falso), \
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

        resultado = _rodar_links(lote=40)
        self.assertEqual(
            {chave: resultado[chave] for chave in ("gerados", "falhas", "pulados")},
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

    def _publicacao(self, status="pendente", idade_horas=0, transport_state=""):
        pub = Publicacao.objects.create(
            usuario=self.user, produto=self.produto, canal="whatsapp",
            destino_id="grupo@g.us", status=status, transport_state=transport_state,
        )
        if idade_horas:
            Publicacao.objects.filter(pk=pub.pk).update(
                criada_em=timezone.now() - timedelta(hours=idade_horas))
        return pub

    @override_settings(SEND_PIPELINE_V2_ENABLED=True, PILOT_ORGANIZATION_IDS=set())
    def test_pendente_antiga_pre_transporte_e_retomavel_quando_ha_fila(self):
        # Retomar exige consumidor: a linha precisa estar na fila v2 E o rollout
        # precisa estar ligado para a organização. Sem isso não há quem processe o
        # reagendamento, e o desfecho correto é terminal (teste abaixo).
        from apps.accounts.models import (
            OrganizationFeatureOverride, ensure_personal_organization,
        )
        OrganizationFeatureOverride.objects.create(
            organization=ensure_personal_organization(self.user),
            feature="SEND_PIPELINE_V2_ENABLED", state="enabled",
        )
        pub = self._publicacao(idade_horas=2, transport_state="queued_v2")

        self.assertEqual(reconciliar_publicacoes_orfas(), 1)

        pub.refresh_from_db()
        self.assertEqual(pub.status, "pendente")
        self.assertEqual(pub.stage, "transport_queued")
        self.assertIsNotNone(pub.next_retry_at)
        self.assertIn("interrompido", pub.erro)

    @override_settings(SEND_PIPELINE_V2_ENABLED=False)
    def test_pendente_antiga_sem_fila_que_a_retome_e_fechada(self):
        pub = self._publicacao(idade_horas=2)

        self.assertEqual(reconciliar_publicacoes_orfas(), 1)

        pub.refresh_from_db()
        self.assertEqual(pub.status, "falhou")
        self.assertEqual(pub.stage, "cancelled")
        self.assertIsNone(pub.next_retry_at)

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

    def test_saude_filtra_por_username(self):
        from apps.scrapers.saude import resumo

        outra_conta = get_user_model().objects.create_user("lules", password="test")
        self._evento("send_failed", level="warning", usuario=self.user)
        self._evento("send_timeout", level="error", pipeline="whatsapp", usuario=outra_conta)

        with patch("apps.scrapers.saude._workers", return_value=[]):
            r = resumo(horas=24, usuario=outra_conta)
        self.assertEqual([(p["pipeline"], p["evento"]) for p in r["problemas"]],
                         [("whatsapp", "send_timeout")])

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
        self._evento("config_pausada", usuario=self.admin)
        self.client.force_login(self.admin)
        with (
            patch("apps.scrapers.saude._workers", return_value=[]),
            patch("apps.scrapers.saude._conexoes_ao_vivo", return_value=[]),
        ):
            resposta = self.client.get(reverse("superadmin-saude"))
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(
            resposta, "Eventos e incidentes da organização de saude-admin",
        )
        self.assertContains(resposta, "Automação pausada sozinha")


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
        from apps.scrapers.health_retest import retest_incident_group
        from apps.scrapers.models import IncidenteSaude

        a = self._incidente(self.u1)
        self._incidente(self.u2)

        with patch("apps.scrapers.conexoes.estado_whatsapp",
                   return_value=_estado_conectado("WhatsApp")):
            retest_incident_group(a.pk)

        self.assertEqual(IncidenteSaude.objects.filter(status="aberto").count(), 0)
        self.assertEqual(IncidenteSaude.objects.filter(status="concluido").count(), 2)

    def test_conexao_de_pe_agora_conclui_o_incidente(self):
        """A causa nº1 de 'Saúde vermelha, dashboard verde'."""
        from apps.scrapers.health_retest import retest_incident_group
        from apps.scrapers.models import IncidenteSaude

        inc = self._incidente(self.u1)

        with patch("apps.scrapers.conexoes.estado_whatsapp",
                   return_value=_estado_conectado("WhatsApp")):
            retest_incident_group(inc.pk)

        inc.refresh_from_db()
        self.assertEqual(inc.status, "concluido")
        self.assertIn("conectado", inc.confirmacao.lower())

    def test_conexao_ainda_caida_mantem_aberto_com_o_motivo(self):
        from apps.scrapers.health_retest import retest_incident_group
        inc = self._incidente(self.u1)

        with patch("apps.scrapers.conexoes.estado_whatsapp",
                   return_value=_estado_caido("WhatsApp", "WhatsApp não está pareado.")):
            result = retest_incident_group(inc.pk)

        inc.refresh_from_db()
        self.assertEqual(inc.status, "aberto")
        self.assertIn("não está pareado", result["message"])

    def test_grupo_parcial_avisa_quantas_faltam(self):
        """Uma conta voltar não pode dar 'tudo certo' quando a outra segue caída."""
        from apps.scrapers.health_retest import retest_incident_group
        from apps.scrapers.models import IncidenteSaude

        a = self._incidente(self.u1)
        self._incidente(self.u2)
        estados = {self.u1: _estado_conectado("WhatsApp"),
                   self.u2: _estado_caido("WhatsApp", "ainda fora")}

        with patch("apps.scrapers.conexoes.estado_whatsapp",
                   side_effect=lambda u, **k: estados[u]):
            retest_incident_group(a.pk)

        self.assertEqual(IncidenteSaude.objects.filter(status="aberto").count(), 1)
        self.assertEqual(IncidenteSaude.objects.filter(status="concluido").count(), 1)

    def test_reteste_preserva_o_filtro(self):
        """O processo web jamais recebe bypass cross-tenant."""
        inc = self._incidente(self.u1)

        r = self.client.post(reverse("superadmin-saude-retestar", args=[inc.pk]))

        self.assertEqual(r.status_code, 503)
        inc.refresh_from_db()
        self.assertEqual(inc.status, "aberto")

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

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        EMAIL_HOST_USER="",
        EMAIL_HOST_PASSWORD="",
    )
    @patch("django.core.mail.get_connection")
    def test_reteste_de_email_nao_aprova_smtp_sem_credenciais(self, get_connection):
        """Abrir o socket do Titan sem login funciona, mas todo envio é recusado.
        O painel não pode transformar esse falso positivo em incidente concluído."""
        from apps.scrapers.views_admin import _retestar_incidente

        inc = self._incidente(
            self.u1, causa="email_falhou", pipeline="sistema", escopo="sistema")

        r = _retestar_incidente(inc)

        self.assertFalse(r["sucesso"])
        self.assertIn("credenciais smtp", r["mensagem"].lower())
        get_connection.assert_not_called()

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        EMAIL_HOST_USER="mailer@example.com",
        EMAIL_HOST_PASSWORD="segredo-de-teste",
    )
    @patch("django.core.mail.get_connection")
    def test_reteste_de_email_valida_login_sem_enviar(self, get_connection):
        from apps.scrapers.views_admin import _retestar_incidente

        inc = self._incidente(
            self.u1, causa="email_falhou", pipeline="sistema", escopo="sistema")

        r = _retestar_incidente(inc)

        self.assertTrue(r["sucesso"])
        get_connection.return_value.open.assert_called_once_with()
        get_connection.return_value.close.assert_called_once_with()

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
        with (
            patch("apps.scrapers.saude._workers", return_value=[]),
            patch("apps.scrapers.saude._conexoes_ao_vivo", return_value=[]),
        ):
            r = self.client.get(reverse("superadmin-saude-json"))

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["estado"], "ok")

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

    def test_fecha_incidente_orfao_de_sessao_de_compra(self):
        from apps.scrapers.incidentes_saude import fechar_conexoes_restabelecidas
        from apps.scrapers.models import IncidenteSaude

        inc = IncidenteSaude.objects.create(
            chave=uuid.uuid4().hex, causa="conexao_caiu", pipeline="conexao",
            escopo="servico:Shopee Compras", usuario=self.user, level="error",
            status="aberto", primeira_ocorrencia=timezone.now(),
            ultima_ocorrencia=timezone.now(), ultima_mensagem="caiu",
            contexto={"servico": "Shopee Compras", "provider": "shopee_shop"},
        )

        with patch(
            "apps.scrapers.report_sessions.has_report_session", return_value=True,
        ):
            self.assertEqual(fechar_conexoes_restabelecidas(), 1)

        inc.refresh_from_db()
        self.assertEqual(inc.status, "concluido")


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
        # Desempate por `-id`. Os dois eventos nascem no mesmo teste e `criado_em` é
        # `auto_now_add`: quando caem no mesmo tique do relógio, ordenar só por
        # `-criado_em` devolve linha arbitrária, e o teste passa ou falha conforme o
        # que rodou antes dele na suíte — foi assim que ele quebrou ao ganharmos um
        # passo a mais dentro de `log_event`. O "mais recente" que este teste quer
        # dizer é o último inserido, e isso o id garante.
        ultimo = eventos.order_by("-criado_em", "-id").first()
        self.assertTrue(ultimo.contexto["repique"])

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
        # O handler do reset não pode recolocar reviveTentado=false depois de
        # descartar a sessão: ele arma os DOIS guardas.
        self.assertIn("suprimirReviveAteQr = true;\n    reviveTentado = true;", html)
        # O único rearme permitido é o do poll, e só numa fase saudável — é ele que
        # devolve um revive a cada novo episódio terminal (ver o teste abaixo).
        self.assertEqual(html.count("reviveTentado = false"), 2)
        self.assertIn("if (faseSaudavel) reviveTentado = false;", html)
        self.assertIn("fase === 'reiniciando_qr'", html)
        self.assertIn("fase === 'qr' && s.qr", html)
        self.assertIn("fase === 'falha_reset'", html)
        self.assertIn("QR novo pronto para leitura.", html)
        self.assertIn("Não foi possível gerar o QR. Clique para tentar novamente.", html)

    def test_front_remove_qr_antigo_e_serializa_polling(self):
        with patch("apps.scrapers.whatsapp_client.status",
                   return_value={"conectado": False, "fase": "qr", "qr": None}):
            response = self.client.get(reverse("scraper-whatsapp"))

        html = response.content.decode()
        self.assertIn("qrImg.removeAttribute('src')", html)
        self.assertIn("refreshQr(null)", html)
        self.assertIn("qrArea.style.display = 'none';\n    refreshQr(null)", html)
        self.assertIn("if (pollEmVoo) return pollEmVoo", html)
        self.assertIn("pollTimer = setTimeout(cicloPoll, 5000)", html)
        self.assertNotIn("setInterval(poll, 5000)", html)
        self.assertIn("cache: 'no-store'", html)


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
        self.assertIn(":site:", site.external_id)

        ativo = por_codigo["ATIVO"]
        self.assertEqual(ativo.coupon_rules["valor_desconto"], 20)
        self.assertEqual(ativo.coupon_rules["valor_minimo"], 49)
        self.assertEqual(ativo.coupon_rules["modo_resgate"], "codigo")
        self.assertEqual(ativo.coupon_rules["container_name"], "x")
        self.assertIsNotNone(ativo.valid_until)

    def test_html_sem_array_devolve_vazio(self):
        from apps.scrapers.sources.ml_public_coupons import _extrair_array_js
        self.assertEqual(_extrair_array_js("<html>sem cupons</html>", "COUPONS"), [])

    def test_corrige_valor_fixo_em_centavos_rotulado_como_percentual(self):
        from apps.scrapers.sources.ml_public_coupons import MLPublicCouponsSource

        html = self.HTML.replace(
            '"nome":"ATIVO"', '"nome":"MELHORPROMO"',
        ).replace(
            '"valor_desconto":"20%","min_compra":"49","desconto_max":"60"',
            '"valor_desconto":"20000%","min_compra":"4399","desconto_max":"200"',
        ).replace('"discount_num":20', '"discount_num":20000', 1)
        src = MLPublicCouponsSource()
        with patch(
            "apps.scrapers.sources.ml_public_coupons.requests.get",
            return_value=Mock(text=html, status_code=200, headers={}, raise_for_status=Mock()),
        ):
            itens = list(src.discover_coupons())

        cupom = next(item for item in itens if item.coupon_code == "MELHORPROMO")
        self.assertEqual(cupom.title, "MELHORPROMO — R$ 200,00 OFF (Fashion)")
        self.assertEqual(cupom.coupon_rules["tipo_desconto"], "fixo")
        self.assertEqual(cupom.coupon_rules["valor_desconto"], 200.0)
        self.assertEqual(cupom.coupon_rules["valor_minimo"], 4399.0)

    def test_rejeita_percentual_impossivel_sem_corroboração_monetaria(self):
        from apps.scrapers.sources.ml_public_coupons import MLPublicCouponsSource

        html = self.HTML.replace('"discount_num":20', '"discount_num":20000', 1).replace(
            '"valor_desconto":"20%"', '"valor_desconto":"20000%"', 1,
        )
        src = MLPublicCouponsSource()
        with patch(
            "apps.scrapers.sources.ml_public_coupons.requests.get",
            return_value=Mock(text=html, status_code=200, headers={}, raise_for_status=Mock()),
        ):
            itens = list(src.discover_coupons())

        self.assertNotIn("ATIVO", {item.coupon_code for item in itens})
        self.assertEqual(src.last_metrics["rejections"]["invalid_discount"], 1)


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
        # Site-wide com mínimo acima do item entra com condição de carrinho.
        self._cupom("a:MIN99", "MIN99", is_mar_aberto=True, discount_num=99, min_compra=500)

        self.assertEqual(_melhor_cupom_normalizado(self.produto), "MIN99")

    def test_produtocupom_confirmado_libera_cupom_de_container(self):
        from apps.scrapers.models import ProdutoCupom
        from apps.scrapers.ofertas import _melhor_cupom_normalizado
        self._cupom("a:SITE20", "SITE20", is_mar_aberto=True, discount_num=20, min_compra=0)
        # Cupom de container de verdade: tem a listagem pública que delimita quais
        # produtos participam. Sem ela, "confirmado" não prova nada — foi assim que
        # um cupom de acessórios automotivos saiu colado num tablet.
        conf = self._cupom(
            "a:CONF40", "CONF40", is_mar_aberto=False, discount_num=40, min_compra=0,
            container_url="https://lista.mercadolivre.com.br/_Container_conf40")
        ProdutoCupom.objects.create(produto=self.produto, cupom=conf, status="confirmado")

        # Confirmado e de maior desconto -> vence o site-wide.
        self.assertEqual(_melhor_cupom_normalizado(self.produto), "CONF40")

    def test_sem_cupom_aplicavel_retorna_none(self):
        from apps.scrapers.ofertas import _melhor_cupom_normalizado
        self._cupom("a:CONT30", "CONT30", is_mar_aberto=False, discount_num=30, min_compra=0)
        self.assertIsNone(_melhor_cupom_normalizado(self.produto))

    def test_compara_percentual_e_fixo_em_reais(self):
        from apps.scrapers.ofertas import _melhor_cupom_normalizado

        self._cupom(
            "a:PCT20", "PCT20", is_mar_aberto=True,
            tipo_desconto="porcentagem", valor_desconto=20, valor_minimo=0,
        )
        self._cupom(
            "a:FIXO15", "FIXO15", is_mar_aberto=True,
            tipo_desconto="fixo", valor_desconto=15, valor_minimo=0,
        )
        self.assertEqual(_melhor_cupom_normalizado(self.produto), "PCT20")

    def test_teto_e_compra_minima_usam_o_preco_de_vitrine(self):
        from apps.scrapers.ofertas import _melhor_cupom_normalizado, montar_mensagem

        self._cupom(
            "a:PCT20", "PCT20", is_mar_aberto=True,
            tipo_desconto="porcentagem", valor_desconto=20,
            desconto_maximo=5, valor_minimo=0,
        )
        self._cupom(
            "a:FIXO15", "FIXO15", is_mar_aberto=True,
            tipo_desconto="fixo", valor_desconto=15, valor_minimo=0,
        )
        self._cupom(
            "a:MIN150", "MIN150", is_mar_aberto=True,
            tipo_desconto="fixo", valor_desconto=99, valor_minimo=150,
        )
        self.assertEqual(_melhor_cupom_normalizado(self.produto), "MIN150")

        mensagem = montar_mensagem(self.produto, "https://meli.la/x", None)
        self.assertIn("CUPOM: MIN150", mensagem)
        self.assertIn("válido em compras acima de R$150", mensagem)

    def test_cupom_restrito_informa_a_condicao_na_mensagem(self):
        from apps.scrapers.ofertas import montar_mensagem

        cupom = self._cupom(
            "a:APP10", "APP10", is_mar_aberto=True,
            tipo_desconto="porcentagem", valor_desconto=10, valor_minimo=0,
            escopo="Somente no app, primeira compra",
        )
        CupomNormalizado.objects.filter(pk=cupom.pk).update(restrito=True)

        mensagem = montar_mensagem(self.produto, "https://meli.la/x", None)
        self.assertIn("CUPOM: APP10", mensagem)
        self.assertIn("Condição:", mensagem)
        self.assertIn("Somente no app, primeira compra", mensagem)


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
        from apps.scrapers.models import CupomFonteObservacao, ProdutoCupom
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
        observacao = CupomFonteObservacao.objects.get(
            cupom=cont, fonte__slug="ml-public-containers",
        )
        self.assertEqual(observacao.outcome, "accepted")
        self.assertEqual(observacao.reason_code, "container_items_proven")
        self.assertEqual(observacao.evidence["product_ids"], 1)
        self.assertNotIn("url", observacao.evidence)

    def test_container_vazio_nao_vira_sucesso_saudavel(self):
        from apps.scrapers.models import CupomFonteObservacao
        from apps.scrapers.scraper_mercadolivre.cupons_container import casar_cupons_container

        cont = self._cupom(
            "a:EMPTY", "EMPTY20", is_mar_aberto=False, discount_num=20,
            container_url="https://lista.mercadolivre.com.br/_Container_empty",
            container_name="empty",
        )

        self.assertEqual(casar_cupons_container(
            coletor=lambda _url, _paginas: set(), max_paginas=1,
        ), 0)

        fonte = FonteIngestao.objects.get(slug="ml-public-containers")
        observacao = CupomFonteObservacao.objects.get(cupom=cont, fonte=fonte)
        self.assertEqual(fonte.status, "degraded")
        self.assertEqual(observacao.health_status, "degraded")
        self.assertEqual(observacao.outcome, "invalid")
        self.assertEqual(observacao.reason_code, "container_empty_unproven")

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
    def test_nao_inventa_relampago_minimo_zero_ou_escopo_emoji(self):
        from apps.scrapers.ofertas import montar_mensagem_cupom

        observado = timezone.now() - timedelta(minutes=7)
        cupom = SimpleNamespace(
            marketplace="amazon", titulo="Cupom da torcida", codigo="TORCIDA30",
            link="https://www.amazon.com.br/", validade=None, relampago=False,
            ultima_observacao=observado,
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 30,
                    "valor_minimo": 0, "modo_resgate": "codigo", "escopo": "🎟"},
            restrito=False,
        )

        mensagem = montar_mensagem_cupom(cupom)

        self.assertIn("*Cupom Amazon*", mensagem)
        self.assertNotIn("⚡", mensagem)
        self.assertNotIn("acima de R$ 0", mensagem)
        self.assertNotIn("Válido para:", mensagem)
        self.assertIn("Fonte checada em", mensagem)
        self.assertIn("Abra a loja e aplique o cupom no checkout:", mensagem)
        self.assertNotIn("Clique no link e navegue", mensagem)

    def test_relampago_real_recebe_selo(self):
        from apps.scrapers.ofertas import montar_mensagem_cupom

        cupom = SimpleNamespace(
            marketplace="shopee", titulo="R$ 20 OFF", codigo="FLASH20",
            link="https://shopee.com.br/", validade=None, relampago=True,
            regras={"tipo_desconto": "fixo", "valor_desconto": 20,
                    "modo_resgate": "codigo"}, restrito=False,
        )

        self.assertIn("Cupom relâmpago ⚡", montar_mensagem_cupom(cupom))

    def test_informa_validade_exata_quando_a_fonte_fornece(self):
        from apps.scrapers.ofertas import montar_mensagem_cupom

        cupom = SimpleNamespace(
            external_id="checkout:HOJE20", marketplace="mercadolivre",
            titulo="20% OFF", codigo="HOJE20", link="https://example.com",
            validade=timezone.now() + timedelta(hours=3),
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 20,
                    "modo_resgate": "codigo"},
            restrito=False,
        )
        mensagem = montar_mensagem_cupom(cupom)

        self.assertIn("Válido até", mensagem)
        self.assertRegex(mensagem, r"\d{2}/\d{2} às \d{2}h\d{2}")

    def test_exibe_produtos_especificos_descritos_no_titulo_oficial(self):
        from apps.scrapers.ofertas import montar_mensagem_cupom
        cupom = SimpleNamespace(
            external_id="campanha:monitor-samsung", marketplace="mercadolivre",
            titulo="R$ 50 OFF em monitores Samsung selecionados", codigo="",
            link="https://lista.mercadolivre.com.br/monitores-samsung",
            regras={"tipo_desconto": "fixo", "valor_desconto": 50,
                    "valor_minimo": 649, "modo_resgate": "ativacao", "escopo": ""},
            restrito=False,
        )

        mensagem = montar_mensagem_cupom(cupom, link_afiliado="https://meli.la/1F4Q5uE")

        self.assertIn("R$ 50 DE DESCONTO acima de R$ 649", mensagem)
        self.assertIn("Válido para:", mensagem)
        self.assertIn("monitores Samsung selecionados", mensagem)
        self.assertIn("Ative o cupom e veja os itens participantes:", mensagem)

    def test_nao_rotula_condicao_de_publico_como_produto(self):
        from apps.scrapers.ofertas import montar_mensagem_cupom
        cupom = SimpleNamespace(
            external_id="awin:restrito", marketplace="awin", anunciante_nome="Loja",
            titulo="30% OFF", codigo="APP30", link="https://awin.example/x",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 30,
                    "modo_resgate": "codigo", "escopo": "Somente no app"},
            restrito=True,
        )

        mensagem = montar_mensagem_cupom(cupom)

        self.assertNotIn("Válido para:", mensagem)
        self.assertIn("Condição:", mensagem)
        self.assertIn("Somente no app", mensagem)

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

    def test_mensagem_com_produtos_da_instrucao_exata_para_cada_resgate(self):
        from apps.scrapers.ofertas import montar_mensagem_cupom_produtos

        produto = SimpleNamespace(
            nome="Notebook confiável", nome_llm="", frase_llm="",
            preco_com_cupom=900,
        )
        relacao = SimpleNamespace(
            preco_atual=1000, preco_final=900, verificado_em=timezone.now(),
        )
        itens = [{
            "produto": produto, "relacao": relacao,
            "link": "https://meli.la/produto",
        }]
        base = {
            "marketplace": "mercadolivre", "titulo": "10% OFF",
            "validade": None, "relampago": False, "restrito": False,
            "ultima_observacao": timezone.now(),
        }

        com_codigo = montar_mensagem_cupom_produtos(SimpleNamespace(
            **base, codigo="NOTE10", regras={"modo_resgate": "codigo"},
        ), itens)
        ativacao = montar_mensagem_cupom_produtos(SimpleNamespace(
            **base, codigo="", regras={"modo_resgate": "ativacao"},
        ), itens)

        self.assertIn("Abra um produto acima e aplique o cupom no checkout.", com_codigo)
        self.assertNotIn("Cupom de ativação", com_codigo)
        self.assertIn("Cupom de ativação", ativacao)
        self.assertIn("confirme o desconto antes de pagar.", ativacao)
        self.assertNotIn("Ative o cupom no link", ativacao)


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
        self.produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Produto comprovado", origem="cupom",
            preco_sem_desconto=150, preco_com_cupom=100,
            link_produto="https://produto.mercadolivre.com.br/MLB-123-produto",
            link_afiliado="https://meli.la/produto", imagem_url="https://img.example/p.jpg",
        )
        from apps.scrapers.coupon_products import atualizar_chave_cupom
        from apps.scrapers.models import CupomPreparacao, ProdutoCupom
        chave = atualizar_chave_cupom(self.cupom)
        ProdutoCupom.objects.create(
            produto=self.produto, cupom=self.cupom, status="confirmado",
            preco_original=150, preco_atual=100, preco_final=80,
            verificado_em=timezone.now())
        CupomPreparacao.objects.create(
            cupom=self.cupom, usuario=None, status="pronto", produtos_chave=chave,
            verificado_em=timezone.now())
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=self.produto, afiliado_ok=True,
            estado="pronto", link_afiliado="https://meli.la/produto",
            verificado_ok=True, verificado_em=timezone.now(),
            url_canonica="https://meli.la/produto",
        )
        self._colagem_patcher = patch("apps.scrapers.colagem.montar_colagem_itens")
        self.colagem = self._colagem_patcher.start()
        self.colagem.side_effect = lambda itens, **_kwargs: (
            "b64", "image/jpeg", list(itens))
        self.addCleanup(self._colagem_patcher.stop)
        # O envio agora pré-checa a conexão do WhatsApp; nestes testes de lógica de
        # cupom o canal é considerado conectado (o transporte é mockado à parte).
        # Testes específicos sobrepõem este patch com um estado desconectado.
        from apps.scrapers.conexoes import Estado
        self._wa_patcher = patch(
            "apps.scrapers.conexoes.estado_whatsapp",
            return_value=Estado(True, "WhatsApp", "worker", "", "", None))
        self._wa_patcher.start()
        self.addCleanup(self._wa_patcher.stop)

    def _sender(self, resultado):
        from apps.scrapers.senders.base import WhatsAppMarkup
        sender = Mock(markup=WhatsAppMarkup(), prefers_image="b64")
        sender.enviar_oferta.return_value = resultado
        return sender

    def test_codigo_sem_produto_comprovado_e_enviado_como_aviso(self):
        from apps.scrapers.models import ProdutoCupom
        from apps.scrapers.ofertas import enviar_cupom

        ProdutoCupom.objects.filter(cupom=self.cupom).delete()
        sender = self._sender({
            "sucesso": True, "via": "whatsapp", "mensagem_id": "code-only-1",
        })
        with patch(
            "apps.scrapers.ofertas.resolver_link_afiliado_cupom",
            return_value={"sucesso": True, "link": "https://meli.la/codigo"},
        ), patch(
            "apps.scrapers.ofertas._preparar_itens_cupom",
            side_effect=AssertionError("código não pode inventar associação a produto"),
        ), patch(
            "apps.scrapers.senders.registry.get_sender", return_value=sender,
        ):
            resultado = enviar_cupom(
                self.cupom, "123@g.us", usuario=self.user,
                imagem_b64_custom="aW1hZ2Vt",
            )

        self.assertTrue(resultado["sucesso"])
        self.assertEqual(resultado["link"], "https://meli.la/codigo")
        args, kwargs = sender.enviar_oferta.call_args
        self.assertIn("SAVE20", args[1])
        self.assertEqual(kwargs["imagem_b64"], "aW1hZ2Vt")
        self.assertFalse(ProdutoCupom.objects.filter(cupom=self.cupom).exists())
        self.assertEqual(
            Publicacao.objects.get(cupom_normalizado=self.cupom).status, "enviado",
        )

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

    def test_produto_sem_link_afiliado_nao_reserva_publicacao(self):
        from apps.scrapers.ofertas import enviar_cupom
        self.produto.link_afiliado = ""
        self.produto.save(update_fields=["link_afiliado"])
        LinkAfiliadoUsuario.objects.filter(
            usuario=self.user, produto=self.produto,
        ).delete()
        sender = self._sender({"sucesso": True})
        with patch("apps.scrapers.senders.registry.get_sender", return_value=sender):
            primeiro = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)
            segundo = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)
        self.assertFalse(primeiro["sucesso"])
        self.assertFalse(segundo.get("duplicado", False))
        self.assertTrue(primeiro["link_afiliado_pendente"])
        self.assertTrue(segundo["link_afiliado_pendente"])
        self.assertFalse(Publicacao.objects.filter(usuario=self.user).exists())

    def test_whatsapp_desconectado_pede_reconexao_antes_de_enviar(self):
        # WhatsApp fora do ar: o envio não pode criar Publicacao nem tentar montar a
        # mensagem — precisa pedir a reconexão com o flag que a UI usa.
        from apps.scrapers.ofertas import enviar_cupom
        from apps.scrapers.conexoes import Estado

        desconectado = Estado(False, "WhatsApp", "worker",
                              "WhatsApp desconectado. Reconecte sua conta.",
                              "sem_pareamento")
        with patch("apps.scrapers.conexoes.estado_whatsapp",
                   return_value=desconectado), \
             patch("apps.scrapers.whatsapp_client.status",
                   return_value={"conectado": False, "fase": "sem_pareamento"}):
            resultado = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)

        self.assertFalse(resultado["sucesso"])
        self.assertTrue(resultado["precisa_login_wa"])
        self.assertEqual(resultado["classe"], "transitorio")
        self.assertFalse(Publicacao.objects.filter(usuario=self.user).exists())

    def test_sessao_ml_caida_reporta_reconexao_e_nao_sem_produtos(self):
        # Havia produtos, mas a sessão do ML caiu ao afiliar: o motivo tem de ser a
        # reconexão do Mercado Livre, não o enganoso "cupom sem produtos".
        from apps.scrapers import ofertas
        from apps.scrapers.ofertas import enviar_cupom

        sender = self._sender({"sucesso": True})
        bloqueio = {"mensagem": "Sessão do Mercado Livre expirada. Reconecte sua conta.",
                    "precisa_login_ml": True}
        with patch("apps.scrapers.senders.registry.get_sender", return_value=sender), \
             patch("apps.scrapers.ofertas._preparar_itens_cupom",
                   return_value=([], bloqueio)):
            resultado = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)

        self.assertFalse(resultado["sucesso"])
        self.assertTrue(resultado["precisa_login_ml"])
        self.assertIn("Mercado Livre", resultado["motivo"])
        self.assertEqual(
            Publicacao.objects.get(cupom_normalizado=self.cupom).status, "falhou")

    def test_link_builder_desativado_nao_pede_reconexao(self):
        """Flag desligada não é sessão caída.

        ML_LINK_BUILDER_ENABLED pode ser desligada por env var (kill switch), e
        antes esse BrowserError era agrupado com as exceções de sessão: o usuário
        lia "Sessão expirada, reconecte" e reconectava em looping uma conta que
        estava perfeita.
        """
        from apps.scrapers.ofertas import enviar_cupom

        sender = self._sender({"sucesso": True})
        bloqueio = {"mensagem": "Link Builder por navegador está desativado para "
                                "esta organização.",
                    "precisa_login_ml": False}
        with patch("apps.scrapers.senders.registry.get_sender", return_value=sender), \
             patch("apps.scrapers.ofertas._preparar_itens_cupom",
                   return_value=([], bloqueio)):
            resultado = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)

        self.assertFalse(resultado["sucesso"])
        self.assertFalse(resultado["precisa_login_ml"])
        self.assertIn("Link Builder", resultado["motivo"])

    def test_cupom_publico_nao_tenta_lock_de_escrita(self):
        """RLS deixa o catálogo público legível, mas não permite FOR UPDATE nele."""
        from apps.scrapers.ofertas import enviar_cupom

        sender = self._sender({"sucesso": True, "via": "whatsapp", "mensagem_id": "m1"})
        with patch.object(CupomNormalizado.objects, "select_for_update",
                          side_effect=AssertionError("cupom público não pode ser bloqueado")), \
             patch("apps.scrapers.senders.registry.get_sender", return_value=sender):
            resultado = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)

        self.assertTrue(resultado["sucesso"])

    def test_cupom_removido_entre_tela_e_reserva_tem_erro_claro(self):
        from apps.scrapers.ofertas import enviar_cupom

        cupom_exibido = self.cupom
        self.cupom.delete()
        resultado = enviar_cupom(cupom_exibido, "123@g.us", usuario=self.user)

        self.assertFalse(resultado["sucesso"])
        self.assertTrue(resultado["cupom_atualizado"])
        self.assertIn("Atualize a tela", resultado["motivo"])
        self.assertFalse(Publicacao.objects.filter(usuario=self.user).exists())

    @patch("apps.scrapers.llm.gerar_conteudo",
           return_value={"titulo": "Oferta especial", "nome_curto": "Produto"})
    def test_cupom_publico_envia_sem_repreparar_ou_gravar_produto(self, _ia):
        """O clique usa o cache pronto e não pode escrever no catálogo RLS."""
        from apps.scrapers.ofertas import enviar_cupom

        sender = self._sender({"sucesso": True, "via": "whatsapp", "mensagem_id": "m1"})
        with patch("apps.scrapers.coupon_products.preparar_cupom",
                   side_effect=AssertionError("envio não prepara catálogo")), \
             patch.object(Produto, "save",
                          side_effect=AssertionError("envio não grava produto público")), \
             patch("apps.scrapers.senders.registry.get_sender", return_value=sender):
            resultado = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)

        self.assertTrue(resultado["sucesso"])
        sender.enviar_oferta.assert_called_once()
        self.assertEqual(Publicacao.objects.get().status, "enviado")

    def test_preparo_vencido_nao_reserva_publicacao(self):
        from apps.scrapers.coupon_products import CACHE_HORAS
        from apps.scrapers.models import CupomPreparacao
        from apps.scrapers.ofertas import enviar_cupom

        # Este gate é exclusivo de ativação. Código digitável sem associação segue
        # o fluxo de aviso de loja e não deve depender de ProdutoCupom.
        self.cupom.codigo = ""
        self.cupom.regras = {**self.cupom.regras, "modo_resgate": "ativacao"}
        self.cupom.save(update_fields=["codigo", "regras"])
        CupomPreparacao.objects.filter(cupom=self.cupom).update(
            verificado_em=timezone.now() - timedelta(hours=CACHE_HORAS, minutes=1))
        resultado = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)

        self.assertFalse(resultado["sucesso"])
        self.assertTrue(resultado["cupom_em_preparo"])
        self.assertIn("sendo atualizado", resultado["motivo"])
        self.assertFalse(Publicacao.objects.filter(usuario=self.user).exists())

    def test_link_afiliado_pendente_nao_reserva_publicacao(self):
        from apps.scrapers.ofertas import enviar_cupom

        self.produto.link_afiliado = ""
        self.produto.save(update_fields=["link_afiliado"])
        LinkAfiliadoUsuario.objects.filter(
            usuario=self.user, produto=self.produto,
        ).delete()
        resultado = enviar_cupom(self.cupom, "123@g.us", usuario=self.user)

        self.assertFalse(resultado["sucesso"])
        self.assertTrue(resultado["link_afiliado_pendente"])
        self.assertIn("links afiliados", resultado["motivo"])
        self.assertFalse(Publicacao.objects.filter(usuario=self.user).exists())

    def test_falha_no_cache_ia_nao_quebra_transacao_externa(self):
        from apps.scrapers.ofertas import _salvar_cache_ia

        with patch.object(Produto, "save", side_effect=DatabaseError("RLS bloqueou update")):
            with transaction.atomic():
                _salvar_cache_ia(self.produto, titulo="Chamada", nome_curto="Nome")
                self.assertTrue(Produto.objects.filter(pk=self.produto.pk).exists())


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

    @patch("apps.scrapers.ofertas.enviar_cupom", side_effect=RuntimeError("falha de teste"))
    def test_sse_de_envio_cupom_nao_esconde_excecao_do_nucleo(self, _enviar):
        fonte = FonteIngestao.objects.create(
            slug="coupon-sse-source", marketplace="mercadolivre", nome="Fonte")
        cupom = CupomNormalizado.objects.create(
            fonte=fonte, external_id="sse-coupon", marketplace="mercadolivre",
            titulo="Cupom SSE", codigo="SSE20",
            regras={"modo_resgate": "codigo"}, estado="ativo",
        )
        CupomDisponibilidade.objects.create(
            organization=self.user.perfil.active_organization,
            usuario=self.user, cupom=cupom, channel="whatsapp",
            use_mode="code_notice", stage="ready",
        )

        response = self.client.post(reverse("scraper-enviar-cupom"), {
            "cupom": cupom.id, "grupo": "123@g.us", "canal": "whatsapp",
        })
        corpo = b"".join(response.streaming_content).decode()

        self.assertIn("O envio encontrou uma falha temporária", corpo)
        self.assertNotIn("Falha inesperada ao processar a solicitação", corpo)

    @patch("apps.scrapers.ofertas.enviar_aviso_cupons")
    def test_sse_de_aviso_avulso_encontra_cupons_prontos_da_organizacao(self, enviar):
        enviar.return_value = {"sucesso": True, "cupons": 1, "via": "whatsapp"}
        fonte = FonteIngestao.objects.create(
            slug="coupon-batch-sse-source", marketplace="mercadolivre", nome="Fonte")
        cupom = CupomNormalizado.objects.create(
            fonte=fonte, external_id="batch-sse-coupon", marketplace="mercadolivre",
            titulo="20% OFF", codigo="LOTE20",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 20},
            estado="ativo", ultima_observacao=timezone.now(),
        )
        CupomDisponibilidade.objects.create(
            organization=self.user.perfil.active_organization,
            usuario=self.user, cupom=cupom, channel="whatsapp",
            use_mode="code_notice", stage="ready",
        )

        response = self.client.post(reverse("scraper-enviar-aviso-cupons"), {
            "marketplace": "mercadolivre", "grupo": "123@g.us",
            "grupo_nome": "Grupo", "canal": "whatsapp",
        })
        corpo = b"".join(response.streaming_content).decode()

        self.assertIn("Aviso com 1 cupom(ns) enviado", corpo)
        self.assertEqual([c.pk for c in enviar.call_args.args[0]], [cupom.pk])

    @patch("apps.scrapers.ofertas.enviar_oferta_de_produto",
           side_effect=RuntimeError("falha de teste"))
    def test_sse_de_envio_produto_nao_esconde_excecao_do_nucleo(self, _enviar):
        produto = Produto.objects.create(
            marketplace="mercadolivre", nome="Oferta SSE", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=60,
            link_produto="https://example.com/p")

        response = self.client.post(reverse("scraper-enviar-produto"), {
            "produto": produto.id, "grupo": "123@g.us", "canal": "whatsapp",
        })
        corpo = b"".join(response.streaming_content).decode()

        self.assertIn("O envio encontrou uma falha temporária", corpo)
        self.assertNotIn("Falha inesperada ao processar a solicitação", corpo)

    def test_cupom_sem_codigo_publico_e_produto_pronto_fica_oculto(self):
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
        self.assertNotIn("&lt;script&gt;", corpo)
        self.assertNotIn('<script>alert("xss")</script>', corpo)
        self.assertNotIn(token, corpo)
        self.assertIn("Nenhum cupom pronto para envio", corpo)


class SemBypassAsyncUnsafeTests(SimpleTestCase):
    """Trava permanente: DJANGO_ALLOW_ASYNC_UNSAFE não pode voltar ao código.

    Ela era usada para chamar o ORM de dentro de `with sync_playwright()`. Como é uma
    variável de ambiente do PROCESSO e o gunicorn roda 8 threads, um fluxo removia a
    permissão debaixo de outro — em produção isso apareceu como
    "Falha na conexão: You cannot call this from an async context". A saída correta é
    tirar a query do bloco do Playwright ou passá-la por
    apps.accounts.tenant.executar_no_tenant.
    """

    def test_nenhum_modulo_de_producao_seta_a_variavel(self):
        # Pela AST, e não por texto: comentários explicando POR QUE o bypass saiu são
        # documentação valiosa e não podem fazer a trava disparar. Só um literal de
        # verdade no código conta.
        import ast
        import pathlib

        raiz = pathlib.Path(__file__).resolve().parent.parent   # apps/
        culpados = []
        for arquivo in raiz.rglob("*.py"):
            if arquivo.name == "tests.py":
                continue          # os testes citam o nome só para provar a ausência
            arvore = ast.parse(arquivo.read_text(encoding="utf-8"))
            for no in ast.walk(arvore):
                if isinstance(no, ast.Constant) and no.value == "DJANGO_ALLOW_ASYNC_UNSAFE":
                    culpados.append(str(arquivo.relative_to(raiz)))
                    break
        self.assertEqual(
            sorted(culpados), [],
            "Use executar_no_tenant ou mova o ORM para fora do `with sync_playwright()` "
            f"em vez de reintroduzir o bypass: {sorted(culpados)}",
        )


class SessaoMLGravadaForaDoPlaywrightTests(TestCase):
    """Regressão central: a sessão só pode ser gravada com o Playwright já fechado.

    O login concluía, `save_storage_state` levantava SynchronousOnlyOperation dentro do
    `with sync_playwright()`, o except genérico jogava o texto cru na tela e a sessão
    NUNCA era salva. É este teste que teria pego o bug em produção.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("ml-live", password="test")
        cache.clear()

    def _playwright_falso(self, ordem):
        """sync_playwright() de mentira que registra quando o bloco fecha."""
        contexto = Mock()
        contexto.storage_state.return_value = {"cookies": [{"domain": ".mercadolivre.com.br"}]}
        pagina = Mock()
        pagina.url = "https://www.mercadolivre.com.br/"
        contexto.pages = [pagina]
        contexto.new_cdp_session.return_value = Mock()
        navegador = Mock()
        navegador.new_context.return_value = contexto

        @contextmanager
        def fake():
            p = Mock()
            p.chromium.launch.return_value = navegador
            try:
                yield p
            finally:
                ordem.append("playwright_fechado")

        return fake

    def test_ml_grava_a_sessao_depois_de_fechar_o_browser(self):
        from apps.scrapers import ml_conexao

        ordem = []
        persistido = {}

        def gravar(user_id, estado, **kwargs):
            ordem.append("sessao_gravada")
            persistido.update(kwargs)

        with patch("playwright.sync_api.sync_playwright", self._playwright_falso(ordem)), \
             patch.object(ml_conexao, "_ir_para_login"), \
             patch("apps.scrapers.conexoes.sondar_sessao_ml",
                   return_value=("conectado", "")), \
             patch.object(ml_conexao, "_persistir_sessao", gravar):
            ml_conexao._worker(self.user.id)

        self.assertEqual(ordem, ["playwright_fechado", "sessao_gravada"])
        self.assertEqual(persistido["prontidao"], "ready")
        self.assertEqual(ml_conexao.status(self.user.id)["fase"], "conectado")

    def test_falha_ao_gravar_nao_deixa_a_tela_presa_em_salvando(self):
        from apps.scrapers import ml_conexao

        with patch("playwright.sync_api.sync_playwright", self._playwright_falso([])), \
             patch.object(ml_conexao, "_ir_para_login"), \
             patch("apps.scrapers.conexoes.sondar_sessao_ml",
                   return_value=("conectado", "")), \
             patch.object(ml_conexao, "_persistir_sessao",
                          side_effect=RuntimeError("banco fora")):
            ml_conexao._worker(self.user.id)

        estado = ml_conexao.status(self.user.id)
        self.assertEqual(estado["fase"], "erro")
        self.assertNotEqual(estado["fase"], "salvando")


class MensagemDeErroDaConexaoTests(SimpleTestCase):
    """A tela recebe uma ação, não o texto interno da exceção.

    O usuário lia literalmente "Falha na conexão: You cannot call this from an async
    context - use a thread or sync_to_async" — sem nenhuma pista do que fazer.
    """

    def test_erro_generico_esconde_o_detalhe_e_mostra_o_codigo(self):
        from apps.scrapers.erros_conexao import mensagem_de_erro

        msg = mensagem_de_erro(Exception("detalhe interno cru"), "ab12")

        self.assertNotIn("detalhe interno cru", msg)
        self.assertIn("ab12", msg)

    def test_regressao_do_bug_async_e_registrada_como_erro(self):
        from django.core.exceptions import SynchronousOnlyOperation
        from apps.scrapers.erros_conexao import mensagem_de_erro

        with self.assertLogs("apps.scrapers.erros_conexao", level="ERROR") as log:
            msg = mensagem_de_erro(SynchronousOnlyOperation("..."), "cd34")

        self.assertIn("cd34", msg)
        self.assertNotIn("async context", msg)
        self.assertIn("executar_no_tenant", "\n".join(log.output))

    def test_mensagem_acionavel_do_goto_e_preservada(self):
        from apps.scrapers.erros_conexao import mensagem_de_erro

        texto = "O Mercado Livre demorou demais a responder a partir do servidor."
        self.assertEqual(mensagem_de_erro(RuntimeError(texto), "ef56"), texto)


class RetryDaGravacaoDaSessaoTests(TestCase):
    """10 minutos de browser ocioso matam o socket do Postgres; uma tentativa só perdia
    o login inteiro por causa disso."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("ml-retry", password="test")

    def test_retenta_apos_falha_de_conexao(self):
        from django.db import OperationalError
        from apps.scrapers import ml_conexao

        chamadas = []

        def instavel(fn, *args, **kwargs):
            chamadas.append(1)
            if len(chamadas) == 1:
                raise OperationalError("server closed the connection unexpectedly")

        with patch.object(ml_conexao, "executar_no_tenant", instavel):
            ml_conexao._persistir_sessao(self.user.id, {"cookies": []})

        self.assertEqual(len(chamadas), 2)

    def test_falha_persistente_propaga(self):
        from django.db import OperationalError
        from apps.scrapers import ml_conexao

        with patch.object(ml_conexao, "executar_no_tenant",
                          side_effect=OperationalError("morto")):
            with self.assertRaises(OperationalError):
                ml_conexao._persistir_sessao(self.user.id, {"cookies": []}, tentativas=2)


class RenovacaoDeSessaoPersistidaTests(TestCase):
    """`iniciar_browser` voltou a renovar cookies.

    O `save_storage_state` do finally rodava DENTRO do `with sync_playwright()` e o
    `except Exception` engolia o SynchronousOnlyOperation — nenhum fluxo renovava
    sessão, e ela envelhecia até o ML revogá-la.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("renova", password="test")

    @contextmanager
    def _cenario(self, ordem, storage_state):
        contexto = Mock()
        contexto.storage_state.return_value = {"cookies": [{"name": "novo"}]}
        navegador = Mock()
        navegador.new_context.return_value = contexto

        @contextmanager
        def playwright_falso():
            p = Mock()
            p.chromium.launch.return_value = navegador
            try:
                yield p
            finally:
                ordem.append("playwright_fechado")

        with patch("apps.scrapers.auxiliar.sync_playwright", playwright_falso), \
             patch("apps.scrapers.auxiliar._iniciar_chromium", return_value=navegador), \
             patch("apps.accounts.ml_sessions.load_storage_state", return_value=storage_state), \
             patch("apps.accounts.ml_sessions.renew_storage_state",
                   side_effect=lambda *a, **k: ordem.append("sessao_gravada")):
            yield

    def test_grava_depois_de_fechar_o_playwright(self):
        from apps.scrapers.auxiliar import iniciar_browser

        ordem = []
        with self._cenario(ordem, {"cookies": [{"name": "velho"}]}):
            with iniciar_browser(session_user=self.user) as (_p, _c):
                pass

        self.assertEqual(ordem, ["playwright_fechado", "sessao_gravada"])

    def test_grava_mesmo_quando_o_corpo_levanta(self):
        from apps.scrapers.auxiliar import iniciar_browser

        ordem = []
        with self._cenario(ordem, {"cookies": [{"name": "velho"}]}):
            with self.assertRaises(ValueError):
                with iniciar_browser(session_user=self.user):
                    raise ValueError("o Link Builder falhou no meio")

        self.assertIn("sessao_gravada", ordem)
        self.assertLess(ordem.index("playwright_fechado"), ordem.index("sessao_gravada"))

    def test_contexto_anonimo_nao_cria_sessao_fantasma(self):
        from apps.scrapers.auxiliar import iniciar_browser

        ordem = []
        with self._cenario(ordem, None):
            with iniciar_browser() as (_p, _c):
                pass

        self.assertNotIn("sessao_gravada", ordem)

    def test_recusa_de_sessao_nao_sobrescreve_a_credencial(self):
        # A tela de login do portal limpa/rotaciona o SSO. Gravar os cookies que ela
        # deixou trocava a credencial boa pela degradada, e a corrida seguinte já
        # partia quebrada — era assim que UMA recusa do Link Builder virava uma
        # sequência delas.
        from apps.scrapers.auxiliar import iniciar_browser
        from apps.scrapers.scraper_mercadolivre.link import LoginError

        ordem = []
        with self._cenario(ordem, {"cookies": [{"name": "velho"}]}):
            with self.assertRaises(LoginError):
                with iniciar_browser(session_user=self.user):
                    raise LoginError("o portal pediu login")

        self.assertNotIn("sessao_gravada", ordem)

    def test_falha_tecnica_continua_renovando(self):
        # A contrapartida do teste acima: timeout/seletor ausente não tocou a
        # autenticação, então os cookies renovados até ali seguem valendo.
        from apps.scrapers.auxiliar import iniciar_browser

        ordem = []
        with self._cenario(ordem, {"cookies": [{"name": "velho"}]}):
            with self.assertRaises(TimeoutError):
                with iniciar_browser(session_user=self.user):
                    raise TimeoutError("o seletor não apareceu")

        self.assertIn("sessao_gravada", ordem)


class ContextoCoerenteDoBrowserTests(TestCase):
    """O Chromium da geração de link era o único que ainda se contradizia.

    `ua_aleatorio()` sorteava Safari/Firefox para um Chromium, sem locale nem fuso —
    exatamente o fingerprint que `contexto_login.py` documenta como causa do
    anti-bot do ML, já corrigido no login e nas sondas HTTP.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("coerente", password="test")

    def test_usa_ua_do_binario_locale_e_fuso(self):
        from apps.scrapers.auxiliar import iniciar_browser

        contexto = Mock()
        contexto.storage_state.return_value = {"cookies": []}
        navegador = Mock()
        navegador.new_context.return_value = contexto
        ua_real = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

        @contextmanager
        def playwright_falso():
            yield Mock()

        with patch("apps.scrapers.auxiliar.sync_playwright", playwright_falso), \
             patch("apps.scrapers.auxiliar._iniciar_chromium", return_value=navegador), \
             patch("apps.scrapers.contexto_login.user_agent_do_binario",
                   return_value=ua_real), \
             patch("apps.accounts.ml_sessions.load_storage_state",
                   return_value={"cookies": []}), \
             patch("apps.accounts.ml_sessions.renew_storage_state"):
            with iniciar_browser(session_user=self.user) as (_p, _c):
                pass

        kwargs = navegador.new_context.call_args.kwargs
        self.assertEqual(kwargs["user_agent"], ua_real)
        self.assertEqual(kwargs["locale"], "pt-BR")
        self.assertEqual(kwargs["timezone_id"], "America/Sao_Paulo")
        self.assertIn("Accept-Language", kwargs["extra_http_headers"])

    def test_o_chamador_ainda_manda_no_contexto(self):
        from apps.scrapers.auxiliar import iniciar_browser

        contexto = Mock()
        contexto.storage_state.return_value = {"cookies": []}
        navegador = Mock()
        navegador.new_context.return_value = contexto

        @contextmanager
        def playwright_falso():
            yield Mock()

        with patch("apps.scrapers.auxiliar.sync_playwright", playwright_falso), \
             patch("apps.scrapers.auxiliar._iniciar_chromium", return_value=navegador), \
             patch("apps.scrapers.contexto_login.user_agent_do_binario",
                   return_value="Chrome/141"):
            with iniciar_browser(locale="en-US") as (_p, _c):
                pass

        self.assertEqual(navegador.new_context.call_args.kwargs["locale"], "en-US")


class SaudeDasFontesTests(TestCase):
    """As duas fontes legadas viviam em "Atenção" por regras estritas demais."""

    def test_ml_com_ofertas_e_sem_cupons_continua_operacional(self):
        # O defeito exato: 800 ofertas + zero produtos na vitrine de cupons (uma
        # página gated por login) marcava degraded para sempre.
        from apps.scrapers.marketplaces.mercadolivre import MercadoLivre

        with patch("apps.scrapers.scraper_mercadolivre.ofertas_scraper.mapear_ofertas",
                   return_value=800), \
             patch("apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper."
                   "mapear_cupons_codigo", return_value=0), \
             patch("apps.scrapers.scraper_mercadolivre.scraper.mapear_cupons",
                   return_value=0), \
             patch("apps.scrapers.scraper_mercadolivre.scraper.projetar_catalogo_cupons"):
            MercadoLivre().scrape_all()

        fonte = FonteIngestao.objects.get(slug="mercadolivre-web")
        self.assertEqual(fonte.status, "ok")
        self.assertIsNotNone(fonte.ultimo_sucesso)
        self.assertEqual(fonte.falhas_consecutivas, 0)
        self.assertIn("vitrine de cupons", fonte.erro_publico)

    def test_ml_sem_ofertas_degrada(self):
        from apps.scrapers.marketplaces.mercadolivre import MercadoLivre

        with patch("apps.scrapers.scraper_mercadolivre.ofertas_scraper.mapear_ofertas",
                   return_value=0), \
             patch("apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper."
                   "mapear_cupons_codigo", return_value=12), \
             patch("apps.scrapers.scraper_mercadolivre.scraper.mapear_cupons",
                   return_value=0), \
             patch("apps.scrapers.scraper_mercadolivre.scraper.projetar_catalogo_cupons"):
            MercadoLivre().scrape_all()

        self.assertEqual(
            FonteIngestao.objects.get(slug="mercadolivre-web").status, "degraded")

    def test_amazon_sem_itens_novos_nao_e_avaria(self):
        # A Creators API repete itens já observados; um ciclo sem novidade é rotina.
        from apps.scrapers.marketplaces.amazon import Amazon

        Amazon._reportar_fonte(timezone.now(), contas=1, falhas=0)

        fonte = FonteIngestao.objects.get(slug="amazon-creators-api")
        self.assertEqual(fonte.status, "ok")
        self.assertIsNotNone(fonte.ultimo_sucesso)
        self.assertEqual(fonte.nome, "Amazon Creators API")

    def test_amazon_com_todas_as_contas_falhando_degrada(self):
        from apps.scrapers.marketplaces.amazon import Amazon

        Amazon._reportar_fonte(timezone.now(), contas=2, falhas=2)

        fonte = FonteIngestao.objects.get(slug="amazon-creators-api")
        self.assertEqual(fonte.status, "degraded")
        self.assertEqual(fonte.falhas_consecutivas, 1)

    def test_falha_da_loja_nao_rebaixa_fonte_que_ja_reportou(self):
        # Uma exceção tardia em scrape_all marcava as TRÊS linhas da Amazon de uma
        # vez, inclusive as que tinham acabado de reportar sucesso.
        from apps.scrapers.management.commands import automacao

        FonteIngestao.objects.create(
            slug="az-ok", marketplace="amazon", nome="Reportou agora",
            status="ok", ultima_tentativa=timezone.now() + timedelta(seconds=5))
        FonteIngestao.objects.create(
            slug="az-mudo", marketplace="amazon", nome="Não reportou",
            status="ok", ultima_tentativa=timezone.now() - timedelta(hours=2))

        loja = Mock()
        loja.scrape_all.side_effect = RuntimeError("a Amazon caiu no fim do ciclo")
        with patch.dict("apps.scrapers.marketplaces.registry.MARKETPLACES",
                        {"amazon": loja}, clear=True), \
             patch.object(automacao, "log_event"), \
             patch("apps.scrapers.maintenance.expire_stale"):
            with self.assertRaises(RuntimeError):
                automacao._rodar_scrape()

        self.assertEqual(FonteIngestao.objects.get(slug="az-ok").status, "ok")
        self.assertEqual(FonteIngestao.objects.get(slug="az-mudo").status, "degraded")


class EvidenciaDeCupomAmazonTests(SimpleTestCase):
    """Duas fontes escrevem a mesma linha de Produto; a prova do cupom não pode
    ser apagada pela que só o deduz por regex."""

    def test_preserva_cupom_ja_confirmado(self):
        from apps.scrapers.sources.persistence import evidencia_com_cupom_preservado

        nova = {"promotion": {"present": True, "label": "Oferta do dia",
                              "coupon_confirmed": False}}
        anterior = {"promotion": {"coupon_confirmed": True,
                                  "label": "Cupom de R$ 20"}}

        resultado = evidencia_com_cupom_preservado(nova, anterior)

        self.assertTrue(resultado["promotion"]["coupon_confirmed"])
        # Só o flag sobe: o resto da evidência nova continua valendo.
        self.assertEqual(resultado["promotion"]["label"], "Oferta do dia")

    def test_sem_prova_anterior_nada_muda(self):
        from apps.scrapers.sources.persistence import evidencia_com_cupom_preservado

        nova = {"promotion": {"coupon_confirmed": False}}

        self.assertIs(evidencia_com_cupom_preservado(nova, None), nova)
        self.assertIs(evidencia_com_cupom_preservado(nova, {}), nova)
        self.assertIs(
            evidencia_com_cupom_preservado(nova, {"promotion": {}}), nova)


class LoteDeLinksResilienteTests(TestCase):
    """O lote parava inteiro no primeiro soluço do Link Builder — e, pior, marcava
    falha nos produtos por erro de INFRAESTRUTURA. Como registrar_falha incrementa
    `tentativas` e aos MAX_TENTATIVAS_ERRO (8) marca estado='erro' com
    proxima_tentativa=None, uma janela de anti-bot podia aposentar dezenas de
    produtos perfeitamente afiliáveis."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("lote-links", password="test")

    def _produtos(self, n):
        return [Produto.objects.create(
            marketplace="mercadolivre", nome=f"Item {i}", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=70,
            link_produto=f"https://produto.mercadolivre.com.br/MLB-{100000+i}",
        ) for i in range(n)]

    def _rodar(self, produtos, afiliar):
        """Roda gerar_links_em_lote com browser e Link Builder falsos."""
        from contextlib import contextmanager
        from apps.scrapers.scraper_mercadolivre import link as ml

        page = MagicMock()
        page.url = "https://www.mercadolivre.com.br/afiliados/linkbuilder#hub"
        # Sem isto, `is_visible()` devolve um MagicMock TRUTHY e _pagina_de_login
        # conclui "tela de login" em toda checagem — o teste passaria por um
        # caminho que não é o que ele quer medir.
        page.get_by_test_id.return_value.is_visible.return_value = False

        @contextmanager
        def _browser(*a, **kw):
            yield page, MagicMock()

        # executar_no_tenant exige contexto de tenant instalado (RLS); no teste ele
        # não existe, então a gravação viraria exceção e mascararia o que se mede.
        direto = lambda fn, *a, **kw: fn(
            *a, **{k: v for k, v in kw.items() if k != "organization_id"}
        )

        with patch.object(ml, "iniciar_browser", _browser), \
             patch.object(ml, "_abrir_link_builder") as abrir, \
             patch.object(ml, "_afiliar_url_na_pagina", side_effect=afiliar), \
             patch.object(ml, "has_storage_state", return_value=True), \
             patch.object(ml, "executar_no_tenant", direto), \
             patch.object(ml, "salvar_cache"), \
             patch.object(ml, "registrar_falha") as falha:
            resultado = ml.gerar_links_em_lote(produtos, usuario=self.user)
        return resultado, falha, abrir, page

    def test_sem_sessao_falha_antes_de_abrir_chromium(self):
        from apps.scrapers.scraper_mercadolivre import link as ml

        produtos = self._produtos(1)
        with patch.object(ml, "iniciar_browser") as navegador:
            with self.assertRaisesMessage(ml.LoginError, "Nenhuma conta"):
                ml.gerar_links_em_lote(produtos, usuario=self.user)
        navegador.assert_not_called()

    def test_falha_no_meio_reabre_e_continua_o_lote(self):
        from apps.scrapers.scraper_mercadolivre import link as ml
        produtos = self._produtos(3)
        chamadas = {"n": 0}

        def afiliar(page, url):
            chamadas["n"] += 1
            if chamadas["n"] == 2:
                # Simula o ML jogando a página para o interstitial no meio do lote.
                page.url = "https://www.mercadolivre.com.br/gz/account-verification?go=x"
                raise RuntimeError("caiu")
            page.url = "https://www.mercadolivre.com.br/afiliados/linkbuilder#hub"
            return "https://meli.la/ok"

        (gerados, falhas), falha, abrir, _ = self._rodar(produtos, afiliar)

        self.assertEqual(gerados, 2)          # antes: 0, o lote inteiro morria
        self.assertEqual(abrir.call_count, 2)  # abertura inicial + 1 reabertura
        falha.assert_not_called()              # erro de infra não queima a fila

    def test_erro_generico_do_portal_nao_queima_tentativa_do_produto(self):
        produtos = self._produtos(2)

        def afiliar(page, url):
            raise RuntimeError("o Link Builder recusou este item")

        (gerados, falhas), falha, _, _ = self._rodar(produtos, afiliar)

        self.assertEqual(gerados, 0)
        self.assertEqual(falhas, 0)
        falha.assert_not_called()

    def test_tres_reaberturas_seguidas_encerram_o_lote(self):
        produtos = self._produtos(10)

        def afiliar(page, url):
            page.url = "https://www.mercadolivre.com.br/gz/account-verification?go=x"
            raise RuntimeError("bloqueado")

        (gerados, falhas), falha, abrir, _ = self._rodar(produtos, afiliar)

        self.assertEqual(gerados, 0)
        falha.assert_not_called()
        # 1 abertura inicial + 3 reaberturas; não gasta as 10 tentativas.
        self.assertEqual(abrir.call_count, 4)

    def test_sessao_morta_de_verdade_aborta_sem_queimar_a_fila(self):
        from apps.scrapers.scraper_mercadolivre import link as ml
        produtos = self._produtos(5)

        def afiliar(page, url):
            page.url = "https://www.mercadolivre.com/jms/mlb/lgz/msl/login/"
            raise RuntimeError("deslogou")

        from contextlib import contextmanager
        page = MagicMock()
        page.url = "https://www.mercadolivre.com.br/afiliados/linkbuilder#hub"
        page.get_by_test_id.return_value.is_visible.return_value = False

        @contextmanager
        def _browser(*a, **kw):
            yield page, MagicMock()

        direto = lambda fn, *a, **kw: fn(
            *a, **{k: v for k, v in kw.items() if k != "organization_id"}
        )
        with patch.object(ml, "iniciar_browser", _browser), \
             patch.object(ml, "_abrir_link_builder",
                          side_effect=[None, ml.LoginError("morreu")]), \
             patch.object(ml, "_afiliar_url_na_pagina", side_effect=afiliar), \
             patch.object(ml, "has_storage_state", return_value=True), \
             patch.object(ml, "executar_no_tenant", direto), \
             patch.object(ml, "salvar_cache"), \
             patch.object(ml, "registrar_falha") as falha:
            with self.assertRaises(ml.LoginError):
                ml.gerar_links_em_lote(produtos, usuario=self.user)
        falha.assert_not_called()


class SentryHookTests(TestCase):
    """Relay Sentry → repository_dispatch (apps.scrapers.hooks)."""

    SEGREDO = "segredo-de-teste"

    def setUp(self):
        self.url = reverse("sentry-hook")
        self.client = Client()

    @contextmanager
    def _configurado(self, **extra):
        env = {
            "SENTRY_HOOK_SECRET": self.SEGREDO,
            "GITHUB_DISPATCH_TOKEN": "ghp_fake",
            "GITHUB_REPO": "g2rmano/Spreading",
        }
        env.update(extra)
        with patch.dict(os.environ, env, clear=False):
            yield

    def _post(self, payload, assinatura=None):
        corpo = json.dumps(payload).encode()
        if assinatura is None:
            assinatura = hmac.new(
                self.SEGREDO.encode(), corpo, hashlib.sha256
            ).hexdigest()
        return self.client.post(
            self.url,
            data=corpo,
            content_type="application/json",
            headers={"sentry-hook-signature": assinatura},
        )

    @staticmethod
    def _payload(**over):
        evento = {
            "issue_id": "4507",
            "title": "KeyError: 'preco'",
            "culprit": "apps.scrapers.ofertas in montar",
            "environment": "production",
            "web_url": "https://sentry.io/organizations/x/issues/4507/",
            "entries": [
                {
                    "type": "exception",
                    "data": {
                        "values": [
                            {
                                "type": "KeyError",
                                "value": "'preco'",
                                "stacktrace": {
                                    "frames": [
                                        {
                                            "filename": "apps/scrapers/ofertas.py",
                                            "lineNo": 120,
                                            "function": "montar",
                                            "vars": {"senha": "hunter2"},
                                        }
                                    ]
                                },
                            }
                        ]
                    },
                }
            ],
        }
        evento.update(over)
        return {"action": "triggered", "data": {"event": evento}}

    def test_assinatura_invalida_e_recusada(self):
        with self._configurado():
            with patch.object(hooks, "_dispara_github") as dispatch:
                resposta = self._post(self._payload(), assinatura="deadbeef")
        self.assertEqual(resposta.status_code, 403)
        dispatch.assert_not_called()

    def test_assinatura_ausente_e_recusada(self):
        with self._configurado():
            with patch.object(hooks, "_dispara_github") as dispatch:
                resposta = self.client.post(
                    self.url,
                    data=json.dumps(self._payload()),
                    content_type="application/json",
                )
        self.assertEqual(resposta.status_code, 403)
        dispatch.assert_not_called()

    def test_sem_credenciais_responde_503(self):
        with patch.dict(
            os.environ,
            {"SENTRY_HOOK_SECRET": "", "GITHUB_DISPATCH_TOKEN": "", "GITHUB_REPO": ""},
            clear=False,
        ):
            with patch.object(hooks, "_dispara_github") as dispatch:
                resposta = self._post(self._payload())
        self.assertEqual(resposta.status_code, 503)
        dispatch.assert_not_called()

    def test_ambiente_fora_de_producao_e_ignorado(self):
        with self._configurado():
            with patch.object(hooks, "_dispara_github") as dispatch:
                resposta = self._post(self._payload(environment="staging"))
        self.assertEqual(resposta.status_code, 204)
        dispatch.assert_not_called()

    def test_evento_sem_issue_id_e_ignorado(self):
        with self._configurado():
            with patch.object(hooks, "_dispara_github") as dispatch:
                resposta = self._post(self._payload(issue_id=None))
        self.assertEqual(resposta.status_code, 204)
        dispatch.assert_not_called()

    def test_acao_diferente_de_triggered_e_ignorada(self):
        payload = self._payload()
        payload["action"] = "created"
        with self._configurado():
            with patch.object(hooks, "_dispara_github") as dispatch:
                resposta = self._post(payload)
        self.assertEqual(resposta.status_code, 204)
        dispatch.assert_not_called()

    def test_dispara_dispatch_com_payload_enxuto(self):
        with self._configurado():
            with patch.object(hooks, "_dispara_github") as dispatch:
                resposta = self._post(self._payload())
        self.assertEqual(resposta.status_code, 202)
        dispatch.assert_called_once()
        despacho = dispatch.call_args.args[0]
        self.assertEqual(despacho["issue_id"], "4507")
        self.assertEqual(despacho["title"], "KeyError: 'preco'")
        self.assertIn("apps/scrapers/ofertas.py:120 in montar", despacho["stack"])
        # O resumo do stacktrace nunca pode carregar variáveis locais.
        self.assertNotIn("hunter2", despacho["stack"])
        self.assertNotIn("senha", despacho["stack"])

    def test_falha_do_github_nao_derruba_o_endpoint(self):
        with self._configurado():
            with patch.object(
                hooks, "_dispara_github", side_effect=OSError("github fora do ar")
            ):
                resposta = self._post(self._payload())
        self.assertEqual(resposta.status_code, 502)

    def test_corpo_invalido_responde_400(self):
        corpo = b"{nao e json"
        assinatura = hmac.new(self.SEGREDO.encode(), corpo, hashlib.sha256).hexdigest()
        with self._configurado():
            resposta = self.client.post(
                self.url,
                data=corpo,
                content_type="application/json",
                headers={"sentry-hook-signature": assinatura},
            )
        self.assertEqual(resposta.status_code, 400)

    def test_get_nao_e_aceito(self):
        with self._configurado():
            self.assertEqual(self.client.get(self.url).status_code, 405)


class AvisoCuponsMensagemTests(SimpleTestCase):
    """O texto tem de sair CARACTERE A CARACTERE como nos modelos da cliente.

    Comparação exata de propósito: o formato foi combinado com ela olhando prints
    de um grupo concorrente, e "quase igual" (um R$ com espaço, um milhar sem
    ponto) é a diferença entre a mensagem parecer profissional ou improvisada.
    """

    @staticmethod
    def _cupom(codigo, **regras):
        base = {"modo_resgate": "codigo"}
        base.update(regras)
        return SimpleNamespace(
            marketplace=regras.pop("marketplace", "mercadolivre"),
            codigo=codigo, titulo=f"Cupom {codigo}", regras=base, restrito=False,
        )

    def test_modelo_do_mercado_livre_com_oito_cupons(self):
        from apps.scrapers.senders.base import Markup
        from apps.scrapers.ofertas import montar_mensagem_aviso_cupons

        cupons = [
            self._cupom("BARATINHO", tipo_desconto="porcentagem", valor_desconto=20,
                        valor_minimo=79, desconto_maximo=60),
            self._cupom("ACHADINHO", tipo_desconto="porcentagem", valor_desconto=15,
                        valor_minimo=199, desconto_maximo=150),
            self._cupom("BRINCAR", tipo_desconto="porcentagem", valor_desconto=15,
                        valor_minimo=59, desconto_maximo=50),
            self._cupom("SUPERPROMO", tipo_desconto="porcentagem", valor_desconto=20,
                        valor_minimo=29, desconto_maximo=500),
            self._cupom("HORADOCUPOM", tipo_desconto="porcentagem", valor_desconto=18,
                        valor_minimo=29, desconto_maximo=500),
            self._cupom("DESCOTOSMELI", tipo_desconto="porcentagem", valor_desconto=25,
                        valor_minimo=29, desconto_maximo=500),
            self._cupom("MAISPORMENOS", tipo_desconto="porcentagem", valor_desconto=22,
                        valor_minimo=29, desconto_maximo=500),
            # Desconto fixo e milhar: 'R$1.499', não 'R$1499' nem 'R$ 1.499'.
            self._cupom("OFFMELIMAIS", tipo_desconto="fixo", valor_desconto=300,
                        valor_minimo=1499),
        ]
        esperado = (
            "🚨 NOVOS CUPONS ML 🚨\n"
            "\n"
            "➡️ 20% OFF em R$79, limitado a R$60 OFF\n"
            "🎟 cupom: BARATINHO\n"
            "\n"
            "➡️ 15% OFF em R$199, limitado a R$150 OFF\n"
            "🎟 cupom: ACHADINHO\n"
            "\n"
            "➡️ 15% OFF em R$59, limitado a R$50 OFF\n"
            "🎟 cupom: BRINCAR\n"
            "\n"
            "➡️ 20% OFF em R$29, limitado a R$500 OFF\n"
            "🎟 cupom: SUPERPROMO\n"
            "\n"
            "➡️ 18% OFF em R$29, limitado a R$500 OFF\n"
            "🎟 cupom: HORADOCUPOM\n"
            "\n"
            "➡️ 25% OFF em R$29, limitado a R$500 OFF\n"
            "🎟 cupom: DESCOTOSMELI\n"
            "\n"
            "➡️ 22% OFF em R$29, limitado a R$500 OFF\n"
            "🎟 cupom: MAISPORMENOS\n"
            "\n"
            "➡️ R$300 OFF em R$1.499\n"
            "🎟 cupom: OFFMELIMAIS\n"
            "\n"
            "Ative em algum produto do link\n"
            "🔗 https://www.mercadolivre.com.br/social/economizanq/lists"
        )

        # Markup neutro: compara o TEXTO, não a marcação de um canal específico.
        mensagem = montar_mensagem_aviso_cupons(
            cupons, "mercadolivre", markup=Markup(),
            link="https://www.mercadolivre.com.br/social/economizanq/lists")

        self.assertEqual(mensagem, esperado)

    def test_modelo_da_amazon_com_um_cupom_so(self):
        from apps.scrapers.senders.base import Markup
        from apps.scrapers.ofertas import montar_mensagem_aviso_cupons

        cupom = self._cupom("SOAMAZON", marketplace="amazon",
                            tipo_desconto="porcentagem", valor_desconto=10,
                            valor_minimo=800, desconto_maximo=150)
        cupom.marketplace = "amazon"
        esperado = (
            "🚨 NOVO CUPOM AMAZON 🚨\n"
            "\n"
            "➡️ 10% OFF em R$800, limitado a R$150 OFF\n"
            "🎟 cupom: SOAMAZON\n"
            "\n"
            "🔗 https://amzn.divulgador.link/Qn1guu7A"
        )

        mensagem = montar_mensagem_aviso_cupons(
            [cupom], "amazon", markup=Markup(),
            link="https://amzn.divulgador.link/Qn1guu7A")

        # Sem "Ative em algum produto do link": na Amazon o código é digitado.
        self.assertEqual(mensagem, esperado)

    def test_whatsapp_marca_cabecalho_desconto_e_codigo(self):
        from apps.scrapers.ofertas import montar_mensagem_aviso_cupons

        cupom = self._cupom("TESTE", tipo_desconto="porcentagem", valor_desconto=10,
                            valor_minimo=50)
        mensagem = montar_mensagem_aviso_cupons([cupom], "mercadolivre", link="http://x")

        self.assertIn("*NOVO CUPOM ML*", mensagem)
        self.assertIn("_10% OFF em R$50_", mensagem)
        self.assertIn("cupom: *TESTE*", mensagem)

    def test_cupom_sem_valor_de_desconto_fica_de_fora(self):
        from apps.scrapers.ofertas import montar_mensagem_aviso_cupons

        sem_valor = self._cupom("VAZIO", tipo_desconto="porcentagem")
        com_valor = self._cupom("VALE", tipo_desconto="porcentagem", valor_desconto=5)

        mensagem = montar_mensagem_aviso_cupons(
            [sem_valor, com_valor], "mercadolivre", link="http://x")

        self.assertNotIn("VAZIO", mensagem)
        self.assertIn("VALE", mensagem)
        # Um cupom sobrou: o cabeçalho volta ao singular.
        self.assertIn("NOVO CUPOM ML", mensagem)

    def test_sem_nenhum_cupom_publicavel_devolve_vazio(self):
        from apps.scrapers.ofertas import montar_mensagem_aviso_cupons

        sem_codigo = SimpleNamespace(
            marketplace="mercadolivre", codigo="", titulo="Campanha",
            regras={"modo_resgate": "ativacao", "tipo_desconto": "porcentagem",
                    "valor_desconto": 20}, restrito=False)

        self.assertEqual(
            montar_mensagem_aviso_cupons([sem_codigo], "mercadolivre", link="http://x"),
            "")


class AvisoCuponsSelecaoTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("aviso-user", password="test")
        self.user.perfil.marcar_verificado()
        self.fonte = FonteIngestao.objects.create(
            slug="aviso-fonte", marketplace="mercadolivre", nome="Cupons ML")
        self.cfg = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="900@g.us", canal="whatsapp",
            marketplace="mercadolivre", tipo=ConfiguracaoEnvio.TIPO_AVISO_CUPONS,
        )

    def _cupom(self, codigo, **extra):
        campos = {"tipo_desconto": "porcentagem", "valor_desconto": 20,
                  "valor_minimo": 50, "modo_resgate": "codigo"}
        campos.update(extra.pop("regras", {}))
        coupon = CupomNormalizado.objects.create(
            fonte=self.fonte, external_id=f"aviso:{codigo}",
            marketplace=extra.pop("marketplace", "mercadolivre"),
            titulo=f"Cupom {codigo}", codigo=codigo, regras=campos,
            estado="ativo", ultima_observacao=timezone.now(), **extra)
        CupomDisponibilidade.objects.create(
            organization=self.cfg.organization, usuario=self.user, cupom=coupon,
            channel=self.cfg.canal, use_mode="code_notice", stage="ready",
        )
        return coupon

    def test_pega_codigo_sem_exigir_produto_preparado(self):
        # O ponto do broadcast: ele NÃO passa pelo portão de associação
        # cupom↔produto, que é o que segurava todo cupom fora das mensagens.
        from apps.scrapers.ofertas import selecionar_cupons_para_aviso

        self._cupom("CODIGO10")
        escolhidos = selecionar_cupons_para_aviso(self.cfg, self.user)

        self.assertEqual([c.codigo for c in escolhidos], ["CODIGO10"])

    def test_disparo_avulso_resolve_organizacao_pelo_usuario(self):
        """O modal não tem ConfiguracaoEnvio salva, mas deve enxergar o funil."""
        from apps.scrapers.ofertas import selecionar_cupons_para_aviso

        self._cupom("AVULSO20")
        avulso = SimpleNamespace(
            marketplace="mercadolivre", grupo_id="900@g.us",
            canal="whatsapp", horas_cooldown=24, incluir_restritos=True,
        )

        escolhidos = selecionar_cupons_para_aviso(avulso, self.user)

        self.assertEqual([c.codigo for c in escolhidos], ["AVULSO20"])

    def test_ignora_cupom_de_ativacao_sem_codigo(self):
        from apps.scrapers.ofertas import selecionar_cupons_para_aviso

        CupomNormalizado.objects.create(
            fonte=self.fonte, external_id="campanha:123", marketplace="mercadolivre",
            titulo="Campanha", codigo="",
            regras={"modo_resgate": "ativacao", "tipo_desconto": "porcentagem",
                    "valor_desconto": 30},
            estado="ativo", ultima_observacao=timezone.now())

        self.assertEqual(selecionar_cupons_para_aviso(self.cfg, self.user), [])

    def test_nao_repete_cupom_ja_anunciado_no_destino(self):
        from apps.scrapers.ofertas import ORIGEM_AVISO_CUPONS, selecionar_cupons_para_aviso

        cupom = self._cupom("JAFOI")
        Publicacao.objects.create(
            usuario=self.user, origem=ORIGEM_AVISO_CUPONS, cupom_normalizado=cupom,
            canal="whatsapp", destino_id=self.cfg.grupo_id,
            status="enviado", enviada_em=timezone.now())

        self.assertEqual(selecionar_cupons_para_aviso(self.cfg, self.user), [])

    def test_respeita_a_loja_da_regra(self):
        from apps.scrapers.ofertas import selecionar_cupons_para_aviso

        self._cupom("SOAMAZON", marketplace="amazon")
        self.assertEqual(selecionar_cupons_para_aviso(self.cfg, self.user), [])

    def test_nao_envia_codigo_que_ainda_nao_chegou_a_ready(self):
        from apps.scrapers.ofertas import selecionar_cupons_para_aviso

        coupon = self._cupom("AGUARDA10")
        CupomDisponibilidade.objects.filter(cupom=coupon).update(
            stage="waiting_link", reason_code="affiliate_link_pending",
        )
        self.assertEqual(selecionar_cupons_para_aviso(self.cfg, self.user), [])


class AvisoCuponsEnvioTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("aviso-envio", password="test")
        self.user.perfil.marcar_verificado()
        self.fonte = FonteIngestao.objects.create(
            slug="aviso-envio-fonte", marketplace="mercadolivre", nome="Cupons ML")
        self.cupom = CupomNormalizado.objects.create(
            fonte=self.fonte, external_id="aviso:ENVIO", marketplace="mercadolivre",
            titulo="Cupom ENVIO", codigo="ENVIO10",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 10, "valor_minimo": 50},
            estado="ativo", ultima_observacao=timezone.now())

    @contextmanager
    def _transporte(self, resultado):
        with patch("apps.scrapers.ofertas._canal_pronto_ou_erro", return_value=None), \
             patch("apps.scrapers.ofertas.resolver_link_afiliado_cupom",
                   return_value={"sucesso": True, "link": "https://meli.la/x"}), \
             patch("apps.scrapers.ofertas.sortear_banner_b64", create=True), \
             patch("apps.scrapers.senders.whatsapp.WhatsAppSender.enviar_oferta",
                   return_value=resultado) as enviar:
            yield enviar

    def test_envia_e_registra_uma_publicacao_por_cupom(self):
        from apps.scrapers.ofertas import ORIGEM_AVISO_CUPONS, enviar_aviso_cupons

        with self._transporte({"sucesso": True, "via": "whatsapp",
                               "mensagem_id": "wa1"}) as enviar:
            resultado = enviar_aviso_cupons(
                [self.cupom], "900@g.us", usuario=self.user, destino_nome="Grupo")

        self.assertTrue(resultado["sucesso"])
        enviar.assert_called_once()
        publicacoes = Publicacao.objects.filter(origem=ORIGEM_AVISO_CUPONS)
        self.assertEqual(publicacoes.count(), 1)
        self.assertEqual(publicacoes.first().status, "enviado")
        self.assertIn("ENVIO10", enviar.call_args.args[1])

    def test_link_banner_e_mensagem_usam_primeiro_cupom_realmente_aceito(self):
        from apps.scrapers.ofertas import enviar_aviso_cupons

        activation = CupomNormalizado.objects.create(
            fonte=self.fonte, external_id="aviso:ATIVACAO",
            marketplace="amazon", titulo="Ativação inválida para aviso",
            codigo="", regras={
                "modo_resgate": "ativacao", "tipo_desconto": "porcentagem",
                "valor_desconto": 50,
            }, estado="ativo", ultima_observacao=timezone.now(),
        )
        resolved = {"sucesso": True, "link": "https://meli.la/codigo-valido"}
        with patch(
            "apps.scrapers.ofertas._canal_pronto_ou_erro", return_value=None,
        ), patch(
            "apps.scrapers.ofertas.resolver_link_afiliado_cupom",
            return_value=resolved,
        ) as resolver, patch(
            "apps.scrapers.banners.sortear_banner_b64", return_value=(None, None),
        ), patch(
            "apps.scrapers.senders.whatsapp.WhatsAppSender.enviar_oferta",
            return_value={"sucesso": True, "via": "whatsapp", "mensagem_id": "wa2"},
        ) as enviar:
            result = enviar_aviso_cupons(
                [activation, self.cupom], "900@g.us", usuario=self.user,
            )

        self.assertTrue(result["sucesso"])
        resolver.assert_called_once_with(self.cupom, self.user)
        sent_message = enviar.call_args.args[1]
        self.assertIn("ENVIO10", sent_message)
        self.assertNotIn("Ativação inválida", sent_message)

    def test_falha_transitoria_agenda_retry_sem_duplicar_publicacao(self):
        from apps.scrapers.ofertas import ORIGEM_AVISO_CUPONS, enviar_aviso_cupons

        with self._transporte({"sucesso": False, "erro": "sem conexão",
                               "classe": "transitorio"}):
            resultado = enviar_aviso_cupons(
                [self.cupom], "900@g.us", usuario=self.user)

        self.assertFalse(resultado["sucesso"])
        publicacao = Publicacao.objects.get(origem=ORIGEM_AVISO_CUPONS)
        self.assertEqual(publicacao.status, "pendente")
        self.assertEqual(publicacao.stage, "transport_queued")
        self.assertIsNotNone(publicacao.next_retry_at)
        self.assertEqual(publicacao.attempt_count, 1)

    def test_sessao_do_ml_caida_e_transitoria_e_nao_freia_a_regra(self):
        # Se isto virasse falha permanente, cinco quedas de sessão seguidas
        # pausariam a automação por um problema que se resolve reconectando.
        from apps.scrapers.ofertas import enviar_aviso_cupons

        with patch("apps.scrapers.ofertas._canal_pronto_ou_erro", return_value=None), \
             patch("apps.scrapers.ofertas.resolver_link_afiliado_cupom",
                   return_value={"sucesso": False, "motivo": "Sessão expirada.",
                                 "precisa_login_ml": True}):
            resultado = enviar_aviso_cupons([self.cupom], "900@g.us", usuario=self.user)

        self.assertFalse(resultado["sucesso"])
        self.assertEqual(resultado["classe"], "transitorio")
        self.assertTrue(resultado["precisa_login_ml"])


class AgendamentoPorDiaTests(SimpleTestCase):
    def test_vazio_significa_todos_os_dias(self):
        cfg = ConfiguracaoEnvio(dias_semana="")
        for dia in range(1, 8):
            agora = timezone.make_aware(
                timezone.datetime(2026, 8, 3 + (dia - 1), 12, 0),  # 03/08/2026 = segunda
                timezone.get_current_timezone())
            self.assertTrue(cfg.dia_permitido(agora), f"dia ISO {dia}")

    def test_so_dias_uteis(self):
        cfg = ConfiguracaoEnvio(dias_semana="1,2,3,4,5")
        tz = timezone.get_current_timezone()
        sexta = timezone.make_aware(timezone.datetime(2026, 8, 7, 12, 0), tz)
        sabado = timezone.make_aware(timezone.datetime(2026, 8, 8, 12, 0), tz)

        self.assertTrue(cfg.dia_permitido(sexta))
        self.assertFalse(cfg.dia_permitido(sabado))

    def test_o_dia_e_o_local_nao_o_utc(self):
        # 22h de sábado em São Paulo já é domingo em UTC. Sem localtime a regra
        # pararia um dia antes do que o usuário marcou.
        cfg = ConfiguracaoEnvio(dias_semana="6")   # só sábado
        sabado_tarde = timezone.make_aware(
            timezone.datetime(2026, 8, 8, 22, 0), timezone.get_current_timezone())

        self.assertEqual(sabado_tarde.astimezone(timezone.UTC).isoweekday(), 7)
        self.assertTrue(cfg.dia_permitido(sabado_tarde))

    def test_o_freio_pula_para_o_proximo_dia_habilitado(self):
        # Freia numa sexta com a regra rodando só de segunda a sexta: o próximo
        # dia habilitado é a segunda, não o sábado.
        cfg = ConfiguracaoEnvio(dias_semana="1,2,3,4,5")
        sexta = timezone.make_aware(
            timezone.datetime(2026, 8, 7, 18, 0), timezone.get_current_timezone())

        cfg.frear(sexta, "grupo sumiu")

        self.assertEqual(timezone.localtime(cfg.pausada_ate).isoweekday(), 1)
        self.assertEqual(cfg.motivo_pausa, "grupo sumiu")


class ListagemPublicaDoCupomTests(SimpleTestCase):
    """O portão de publicabilidade não pode ser mais estrito que o do preparo.

    Em produção, 2062 dos 2073 cupons de campanha do ML eram reprovados por
    `container_url` vazio — enquanto o `link` deles já era a listagem pública da
    campanha, que `coupon_products._coletar_ml_remoto` aceitava para preparar o
    MESMO cupom. Era catálogo publicável sendo descartado, não segurança.
    """

    @staticmethod
    def _cupom(link="", container_url="", **extra):
        regras = {"modo_resgate": "ativacao", "tipo_desconto": "porcentagem",
                  "valor_desconto": 20, "container_url": container_url}
        regras.update(extra.pop("regras", {}))
        return SimpleNamespace(
            marketplace="mercadolivre", codigo="", link=link, titulo="Campanha",
            external_id="campanha:123", regras=regras,
            fonte=SimpleNamespace(slug="mercadolivre-web"), owner=None, **extra)

    def test_aceita_o_link_de_listagem_da_campanha(self):
        from apps.scrapers.coupon_rules import listagem_publica_ml

        url = "https://lista.mercadolivre.com.br/_CustId_353130616?coupon_campaign_id=13998892"
        self.assertEqual(listagem_publica_ml(self._cupom(link=url)), url)

    def test_aceita_container_e_prefere_ele_ao_link(self):
        from apps.scrapers.coupon_rules import listagem_publica_ml

        container = "https://lista.mercadolivre.com.br/_Container_13011675"
        cupom = self._cupom(link="https://lista.mercadolivre.com.br/outra",
                            container_url=container)
        self.assertEqual(listagem_publica_ml(cupom), container)

    def test_recusa_a_vitrine_generica_de_cupons(self):
        # É o link de fallback da projeção de campanhas e não prova escopo nenhum.
        from apps.scrapers.coupon_rules import listagem_publica_ml

        self.assertEqual(
            listagem_publica_ml(
                self._cupom(link="https://www.mercadolivre.com.br/cupons")),
            "")

    def test_recusa_host_parecido(self):
        # `endswith`/`in` numa string crua aceitaria este domínio.
        from apps.scrapers.coupon_rules import listagem_publica_ml

        self.assertEqual(
            listagem_publica_ml(
                self._cupom(link="https://lista.mercadolivre.com.br.evil.com/_Container_1")),
            "")

    def test_recusa_esquema_nao_http(self):
        from apps.scrapers.coupon_rules import listagem_publica_ml

        self.assertEqual(
            listagem_publica_ml(self._cupom(link="javascript:alert(1)")), "")

    @patch("apps.accounts.feature_flags.enabled_for_user", return_value=True)
    def test_campanha_com_link_de_lista_vira_publicavel(self, _enabled):
        from apps.scrapers.coupon_rules import ativacao_publicavel

        cupom = self._cupom(
            link="https://lista.mercadolivre.com.br/_CustId_1?coupon_campaign_id=2")
        self.assertTrue(ativacao_publicavel(cupom))

    @override_settings(ML_CUPONS_ATIVACAO_ENABLED=True, PILOT_ORGANIZATION_IDS=set())
    def test_campanha_sem_listagem_continua_reprovada(self):
        from apps.scrapers.coupon_rules import ativacao_publicavel

        self.assertFalse(ativacao_publicavel(
            self._cupom(link="https://www.mercadolivre.com.br/cupons")))


class EscopoDeTenantNasChecagensDeConexaoTests(TestCase):
    """Regressão: tela verde e envio dizendo "desconectado" (WhatsApp e ML).

    Os streams de envio passaram a rodar com `segurar_transacao=False`, em que o
    tenant fica apenas ANOTADO — nenhum GUC instalado na conexão. As queries
    visíveis foram convertidas para `executar_no_tenant`, mas as que vivem DENTRO
    das checagens de conexão continuaram nuas: sob RLS elas voltam zero linhas e
    cada checagem conclui "não conectado", enquanto a tela — que roda dentro de uma
    request, com escopo instalado — mostra tudo conectado.

    O suite roda em SQLite, onde não existe RLS: o que se observa aqui é o escopo
    vigente NO MOMENTO da query. Escopo ausente é exatamente o que a RLS pune em
    produção.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("escopo-conexao", password="test")
        self.organization = self.user.personal_organization
        self.sessao = self.user.perfil.sessao_whatsapp()

    @contextmanager
    def _espiar_escopo(self, alvo, atributo, modelo):
        """Troca `alvo.<atributo>` por um espião que anota o escopo e delega ao real."""
        from apps.accounts.tenant import current_organization_id

        visto = {"escopo": "__nao_consultado__"}
        gerente = modelo.objects

        class _Espiao:
            def __getattr__(_self, nome):
                visto["escopo"] = current_organization_id()
                return getattr(gerente, nome)

        with patch.object(alvo, atributo, SimpleNamespace(objects=_Espiao())):
            yield visto

    # ── WhatsApp ──

    def test_capability_e_emitida_com_o_tenant_instalado(self):
        from apps.accounts import wa_capabilities
        from apps.accounts.models import WhatsAppConnection
        from apps.accounts.tenant import tenant_suspenso

        with self._espiar_escopo(
            wa_capabilities, "WhatsAppConnection", WhatsAppConnection,
        ) as visto:
            with tenant_suspenso(self.organization.pk, actor_id=self.user.pk):
                token = wa_capabilities.issue_capability(self.sessao, ["status"])

        self.assertTrue(token)
        self.assertEqual(visto["escopo"], str(self.organization.pk))

    @override_settings(WHATSAPP_WEB_ENABLED=True)
    def test_flag_do_whatsapp_le_o_vinculo_com_o_tenant_instalado(self):
        from apps.accounts import feature_flags
        from apps.accounts.models import WhatsAppConnection
        from apps.accounts.tenant import tenant_suspenso

        with override_settings(PILOT_ORGANIZATION_IDS={str(self.organization.pk)}):
            with self._espiar_escopo(
                feature_flags, "WhatsAppConnection", WhatsAppConnection,
            ) as visto:
                with tenant_suspenso(self.organization.pk, actor_id=self.user.pk):
                    liberado = feature_flags.enabled_for_whatsapp_session(self.sessao)

        self.assertTrue(liberado)
        self.assertEqual(visto["escopo"], str(self.organization.pk))

    @override_settings(ML_LINK_BUILDER_ENABLED=True)
    def test_flag_por_usuario_resolve_a_organizacao_com_o_tenant_instalado(self):
        from apps.accounts import feature_flags
        from apps.accounts.tenant import current_organization_id, tenant_suspenso
        from apps.accounts.models import organization_for_user

        visto = {}

        def _espiao(user):
            visto["escopo"] = current_organization_id()
            return organization_for_user(user)

        with override_settings(PILOT_ORGANIZATION_IDS={str(self.organization.pk)}):
            with patch.object(feature_flags, "organization_for_user", _espiao):
                with tenant_suspenso(self.organization.pk, actor_id=self.user.pk):
                    liberado = feature_flags.enabled_for_user(
                        "ML_LINK_BUILDER_ENABLED", self.user)

        self.assertTrue(liberado)
        self.assertEqual(visto["escopo"], str(self.organization.pk))

    @override_settings(WHATSAPP_WEB_ENABLED=True, PILOT_ORGANIZATION_IDS=set())
    def test_gate_de_envio_aprova_whatsapp_conectado_sob_tenant_suspenso(self):
        from apps.accounts.tenant import tenant_suspenso

        whatsapp_client.invalidar_status(self.sessao)
        with patch.object(whatsapp_client, "_request_json",
                          return_value={"conectado": True, "fase": "conectado"}):
            with tenant_suspenso(self.organization.pk, actor_id=self.user.pk):
                erro = ofertas._canal_pronto_ou_erro("whatsapp", self.user)

        self.assertIsNone(erro)

    def test_falha_de_autorizacao_nao_pede_para_reler_o_qr(self):
        """"Não falamos com o worker" != "seu pareamento caiu".

        Marcar precisa_login_wa aqui mandava o usuário reescanear um QR Code que
        estava perfeito, sem nunca revelar que o defeito era do nosso lado.
        """
        from apps.scrapers.conexoes import Estado

        fora = Estado(False, "WhatsApp", "worker",
                      "Não foi possível falar com o serviço de WhatsApp.",
                      "servico_fora")
        with patch("apps.scrapers.conexoes.estado_whatsapp", return_value=fora):
            erro = ofertas._canal_pronto_ou_erro("whatsapp", self.user)

        self.assertIsNotNone(erro)
        self.assertFalse(erro.get("precisa_login_wa"))
        self.assertEqual(erro["classe"], ofertas.TRANSITORIO)

    # ── Mercado Livre ──

    def _produto(self):
        return Produto.objects.create(
            marketplace="mercadolivre", nome="Item escopo", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=70,
            link_produto="https://produto.mercadolivre.com.br/MLB-556677",
        )

    def test_link_em_cache_e_lido_com_o_tenant_instalado(self):
        """Sem escopo o cache some, o gate seguinte diz "sem sessão" e o envio
        acusa "Sessão do Mercado Livre expirada" com a tela de conexão verde."""
        from apps.accounts.tenant import tenant_suspenso
        from apps.scrapers.marketplaces.registry import get_marketplace

        produto = self._produto()
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto,
            link_afiliado="https://meli.la/escopo", afiliado_ok=True,
            verificado_ok=True, estado="pronto",
            url_canonica="https://meli.la/escopo",
        )

        with tenant_suspenso(self.organization.pk, actor_id=self.user.pk):
            info = get_marketplace("mercadolivre").build_affiliate_link(
                produto, usuario=self.user)

        self.assertEqual(info["link_afiliado"], "https://meli.la/escopo")

    def test_sessao_do_ml_e_consultada_com_o_tenant_instalado(self):
        from apps.accounts.tenant import current_organization_id, tenant_suspenso
        from apps.scrapers.scraper_mercadolivre import link as ml

        produto = self._produto()
        visto = {}

        def _espiao(user):
            visto["escopo"] = current_organization_id()
            return False

        with patch.object(ml, "has_storage_state", _espiao):
            with tenant_suspenso(self.organization.pk, actor_id=self.user.pk):
                with self.assertRaises(ml.LoginError):
                    ml.gerar_link_afiliado_para_produto(produto, usuario=self.user)

        self.assertEqual(visto["escopo"], str(self.organization.pk))

    # ── O caminho inteiro, como roda em produção ──

    @contextmanager
    def _rls_simulada(self, alvo, atributo, modelo):
        """Emula a policy do Postgres: sem escopo instalado, a tabela some.

        O suite roda em SQLite, onde RLS não existe — sem esta emulação o teste
        passaria com e sem a correção, que é exatamente o que deixou o bug chegar
        em produção.
        """
        from apps.accounts.tenant import current_organization_id

        gerente = modelo.objects

        class _SobPolicy:
            def __getattr__(_self, nome):
                visivel = gerente if current_organization_id() else gerente.none()
                return getattr(visivel, nome)

        with patch.object(alvo, atributo, SimpleNamespace(objects=_SobPolicy())):
            yield

    @override_settings(WHATSAPP_WEB_ENABLED=True, PILOT_ORGANIZATION_IDS=set())
    def test_envio_no_runner_real_nao_acusa_desconexao(self):
        """Exatamente o que o usuário vê: o núcleo do envio rodando dentro do alvo de
        thread do SSE, com `segurar_transacao=False` e a RLS valendo. O cupom até pode
        não sair (falta link preparado), mas o motivo NUNCA pode ser "reconecte sua
        conta" — e o worker tem de ter sido realmente consultado."""
        from apps.accounts import wa_capabilities
        from apps.accounts.models import WhatsAppConnection
        from apps.accounts.tenant import organization_thread_target

        fonte = FonteIngestao.objects.create(
            slug="escopo-runner-fonte", marketplace="mercadolivre", nome="Cupons ML")
        cupom = CupomNormalizado.objects.create(
            fonte=fonte, external_id="escopo:RUNNER", marketplace="mercadolivre",
            titulo="Cupom RUNNER", codigo="RUNNER10",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 10},
            estado="ativo", ultima_observacao=timezone.now())

        resultado = {}

        def _job():
            resultado["envio"] = ofertas.enviar_cupom(
                cupom, "900@g.us", canal="whatsapp", usuario=self.user,
                destino_nome="Grupo")

        whatsapp_client.invalidar_status(self.sessao)
        with patch.object(whatsapp_client, "_request_json",
                          return_value={"conectado": True, "fase": "conectado"}) as worker, \
             self._rls_simulada(wa_capabilities, "WhatsAppConnection", WhatsAppConnection):
            organization_thread_target(
                self.organization, _job, segurar_transacao=False)()

        envio = resultado["envio"]
        # Sem a correção a capability nem é emitida: o gate desiste antes de existir
        # qualquer pergunta ao worker.
        self.assertTrue(worker.called, "o worker WhatsApp nunca foi consultado")
        self.assertFalse(envio.get("precisa_login_wa"), envio.get("motivo"))
        self.assertFalse(envio.get("precisa_login_ml"), envio.get("motivo"))
        self.assertNotIn("Reconecte", envio.get("motivo") or "")
        self.assertNotIn("desconectado", (envio.get("motivo") or "").lower())


class PermissaoEnvioNoPainelTests(TestCase):
    """Conceder a permissão lê o Perfil de OUTRO usuário — e a RLS esconde isso.

    O processo web não tem bypass: `user.perfil` de um terceiro levanta
    RelatedObjectDoesNotExist e a tela devolvia 500. A policy aceita
    `user_id = app.actor_id`, então o painel trabalha no escopo do alvo.
    """

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            "envio-admin", password="test", is_staff=True, is_superuser=True)
        self.alvo = User.objects.create_user("envio-alvo", password="test")
        self.client.force_login(self.admin)

    @contextmanager
    def _perfil_sob_rls(self):
        """Emula a policy: o Perfil só existe dentro do actor_context do dono.

        Em SQLite não há RLS — sem esta emulação o teste passaria com e sem a
        correção, que foi exatamente o que deixou o 500 chegar em produção.
        """
        from apps.accounts.models import Perfil
        from apps.accounts.tenant import current_actor_id
        from apps.scrapers import views_admin

        User = get_user_model()
        gerente = Perfil.objects
        nao_existe = User.perfil.RelatedObjectDoesNotExist

        class _SobPolicy:
            def __getattr__(_self, nome):
                visivel = gerente if current_actor_id() else gerente.none()
                return getattr(visivel, nome)

        def _descriptor(usuario):
            if current_actor_id() != str(usuario.pk):
                raise nao_existe("User has no perfil.")
            return gerente.get(user=usuario)

        with patch.object(views_admin, "Perfil", SimpleNamespace(objects=_SobPolicy())), \
                patch.object(User, "perfil", property(_descriptor)):
            yield

    def test_concede_permissao_com_a_policy_valendo(self):
        with self._perfil_sob_rls():
            resposta = self.client.post(
                reverse("superadmin-permissao-envio", args=[self.alvo.pk]))

        self.assertEqual(resposta.status_code, 302)
        self.alvo.refresh_from_db()
        self.assertTrue(self.alvo.perfil.pode_ligar_envio)

    def test_tela_mostra_a_permissao_ja_concedida(self):
        perfil = self.alvo.perfil
        perfil.pode_ligar_envio = True
        perfil.save(update_fields=["pode_ligar_envio"])

        with self._perfil_sob_rls(), \
                patch("apps.scrapers.conexoes.estados_do_usuario", return_value=[]):
            resposta = self.client.get(
                reverse("superadmin-usuario", args=[self.alvo.pk]))

        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, "Revogar controle do envio automático")
