"""Contrato completo do onboarding de QR dos dois logins do Mercado Livre."""
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse


class DetectorDoDesafioDeCameraTests(SimpleTestCase):
    class Page:
        def __init__(self, url="https://www.mercadolivre.com.br/", texto=""):
            self.url = url
            self.texto = texto

        def evaluate(self, _script):
            return self.texto.lower()

    def test_url_oficial_e_autoritativa(self):
        from apps.scrapers.ml_login_challenge import pagina_exige_configuracao_qr

        page = self.Page(
            "https://www.mercadolivre.com/jms/mlb/lgz/msl/login/camera-not-found?x=1"
        )
        self.assertTrue(pagina_exige_configuracao_qr(page))

    def test_texto_em_portugues_e_fallback(self):
        from apps.scrapers.ml_login_challenge import pagina_exige_configuracao_qr

        page = self.Page(texto=(
            "Seu computador não tem uma câmera para iniciar sessão com "
            "reconhecimento facial. Ative o código QR."
        ))
        self.assertTrue(pagina_exige_configuracao_qr(page))

    def test_texto_em_espanhol_e_fallback(self):
        from apps.scrapers.ml_login_challenge import pagina_exige_configuracao_qr

        page = self.Page(texto=(
            "Tu computadora no tiene una cámara para iniciar sesión con "
            "reconocimiento facial. Activa el código QR."
        ))
        self.assertTrue(pagina_exige_configuracao_qr(page))

    def test_login_normal_nao_e_desafio(self):
        from apps.scrapers.ml_login_challenge import pagina_exige_configuracao_qr

        page = self.Page(texto="Entre na sua conta com e-mail e senha")
        self.assertFalse(pagina_exige_configuracao_qr(page))

    def test_mencao_isolada_a_camera_nao_e_falso_positivo(self):
        from apps.scrapers.ml_login_challenge import pagina_exige_configuracao_qr

        page = self.Page(texto="Use a câmera para ler um código QR de pagamento")
        self.assertFalse(pagina_exige_configuracao_qr(page))

    def test_falha_durante_navegacao_e_tolerada(self):
        from apps.scrapers.ml_login_challenge import pagina_exige_configuracao_qr

        page = Mock()
        type(page).url = property(lambda _self: (_ for _ in ()).throw(RuntimeError("navegando")))
        page.evaluate.side_effect = RuntimeError("contexto destruído")
        self.assertFalse(pagina_exige_configuracao_qr(page))


class EstadoDoOnboardingQRTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_primeira_deteccao_publica_estado_estruturado(self):
        from apps.scrapers import ml_conexao

        ml_conexao._marcar_configuracao_qr(17, "login")
        state = cache.get(ml_conexao._cache_key(17))
        self.assertEqual(state["fase"], "configurar_qr")
        self.assertEqual(state["desafio"], {
            "tipo": "camera_indisponivel", "contexto": "login", "tentativas": 1,
        })

    def test_poll_repetido_na_mesma_tela_nao_incrementa_tentativa(self):
        from apps.scrapers import ml_conexao

        ml_conexao._marcar_configuracao_qr(18, "login")
        ml_conexao._marcar_configuracao_qr(18, "login")
        self.assertEqual(
            cache.get(ml_conexao._cache_key(18))["desafio"]["tentativas"], 1,
        )

    def test_desafio_que_reaparece_depois_do_retry_incrementa(self):
        from apps.scrapers import ml_conexao

        ml_conexao._marcar_configuracao_qr(19, "login")
        ml_conexao._set_estado(19, fase="aguardando_login")
        ml_conexao._marcar_configuracao_qr(19, "login")
        self.assertEqual(
            cache.get(ml_conexao._cache_key(19))["desafio"]["tentativas"], 2,
        )

    def test_contexto_novo_recomeca_contagem(self):
        from apps.scrapers import ml_conexao

        ml_conexao._marcar_configuracao_qr(20, "login")
        ml_conexao._set_estado(20, fase="validando_linkbuilder")
        ml_conexao._marcar_configuracao_qr(20, "linkbuilder")
        desafio = cache.get(ml_conexao._cache_key(20))["desafio"]
        self.assertEqual(desafio["contexto"], "linkbuilder")
        self.assertEqual(desafio["tentativas"], 1)

    def test_status_nao_expoe_flag_de_comando_nem_sonda(self):
        from apps.scrapers import ml_conexao

        ml_conexao._set_estado(
            21, fase="configurar_qr", retentar_qr=True, cancelar=True,
            salvar_agora=True, validar_agora=True,
            desafio={"tipo": "camera_indisponivel", "contexto": "login", "tentativas": 1},
        )
        with patch.object(ml_conexao, "_transport", Mock(status=Mock(return_value={}))), \
             patch("apps.scrapers.conexoes.estado_ml") as sonda:
            state = ml_conexao.status(21)
        for comando in ("retentar_qr", "cancelar", "salvar_agora", "validar_agora"):
            self.assertNotIn(comando, state)
        self.assertEqual(state["fase"], "configurar_qr")
        sonda.assert_not_called()

    def test_relatorios_nao_expoem_flag_de_comando(self):
        from apps.scrapers import ml_relatorio_conexao

        ml_relatorio_conexao._set(
            22, fase="configurar_qr", retentar_qr=True, cancelar=True,
            salvar_agora=True, validar_agora=True,
            desafio={"tipo": "camera_indisponivel", "contexto": "relatorios", "tentativas": 1},
        )
        with patch.object(ml_relatorio_conexao, "has_report_session", return_value=False), \
             patch.object(ml_relatorio_conexao, "_transport", Mock(status=Mock(return_value={}))), \
             patch("django.contrib.auth.get_user_model") as user_model:
            user_model.return_value.objects.filter.return_value.first.return_value = None
            state = ml_relatorio_conexao.status(22)
        for comando in ("retentar_qr", "cancelar", "salvar_agora", "validar_agora"):
            self.assertNotIn(comando, state)
        self.assertEqual(state["desafio"]["contexto"], "relatorios")


class RetomadaDoOnboardingQRTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    @staticmethod
    def _thread(viva=True):
        return Mock(is_alive=Mock(return_value=viva))

    def test_principal_exige_worker_ativo(self):
        from apps.scrapers import ml_conexao

        ml_conexao._set_estado(30, fase="configurar_qr")
        with patch.dict(ml_conexao._threads, {}, clear=True):
            ok, payload = ml_conexao.retentar_apos_configurar_qr(30)
        self.assertFalse(ok)
        self.assertNotIn("Playwright", payload["erro"])

    def test_principal_aceita_somente_na_fase_correta(self):
        from apps.scrapers import ml_conexao

        ml_conexao._set_estado(31, fase="aguardando_login")
        with patch.dict(ml_conexao._threads, {31: self._thread()}, clear=True), \
             patch.object(ml_conexao._transport, "get", return_value=Mock()):
            ok, payload = ml_conexao.retentar_apos_configurar_qr(31)
        self.assertFalse(ok)
        self.assertIn("não está aguardando", payload["erro"])

    def test_principal_sinaliza_sem_expor_flag(self):
        from apps.scrapers import ml_conexao

        ml_conexao._set_estado(32, fase="configurar_qr")
        with patch.dict(ml_conexao._threads, {32: self._thread()}, clear=True), \
             patch.object(ml_conexao._transport, "get", return_value=Mock()), \
             patch.object(ml_conexao._transport, "status", return_value={}):
            ok, payload = ml_conexao.retentar_apos_configurar_qr(32)
        self.assertTrue(ok)
        self.assertNotIn("retentar_qr", payload)
        self.assertTrue(cache.get(ml_conexao._cache_key(32))["retentar_qr"])

    def test_relatorios_sinalizam_a_mesma_sessao(self):
        from apps.scrapers import ml_relatorio_conexao

        ml_relatorio_conexao._set(33, fase="configurar_qr")
        with patch.dict(ml_relatorio_conexao._threads, {33: self._thread()}, clear=True), \
             patch.object(ml_relatorio_conexao._transport, "get", return_value=Mock()), \
             patch.object(ml_relatorio_conexao._transport, "status", return_value={}), \
             patch.object(ml_relatorio_conexao, "has_report_session", return_value=False), \
             patch("django.contrib.auth.get_user_model") as user_model:
            user_model.return_value.objects.filter.return_value.first.return_value = None
            ok, payload = ml_relatorio_conexao.retentar_apos_configurar_qr(33)
        self.assertTrue(ok)
        self.assertEqual(payload["fase"], "configurar_qr")
        self.assertNotIn("retentar_qr", payload)


class FluxosReaisDoWorkerQRTests(TestCase):
    CAMERA_URL = "https://www.mercadolivre.com/jms/mlb/lgz/msl/login/camera-not-found"

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username=f"qr-worker-{time.time_ns()}", password="test",
        )
        cache.clear()

    @staticmethod
    def _playwright_falso(page, context):
        browser = Mock()
        browser.new_context.return_value = context

        @contextmanager
        def fake():
            runtime = Mock()
            runtime.chromium.launch.return_value = browser
            yield runtime

        return fake

    @staticmethod
    def _contexto(page):
        context = Mock()
        context.pages = [page]
        context.storage_state.return_value = {
            "cookies": [{"domain": ".mercadolivre.com.br", "name": "ssid", "value": "x"}],
            "origins": [],
        }
        return context

    @staticmethod
    def _page(url):
        page = Mock()
        page.url = url
        page.is_closed.return_value = False
        page.screenshot.return_value = b"jpeg"
        page.evaluate.return_value = ""
        page.locator.return_value.count.return_value = 0
        page.wait_for_timeout.side_effect = lambda _ms: time.sleep(0.005)
        return page

    def _responder_ao_guia(self, key, setter, observado, contexto):
        limite = time.time() + 3
        while time.time() < limite:
            state = cache.get(key) or {}
            if state.get("fase") == "configurar_qr":
                if state.get("desafio", {}).get("contexto") != contexto:
                    time.sleep(0.01)
                    continue
                observado.append(dict(state))
                setter(retentar_qr=True)
                return
            time.sleep(0.01)

    def test_login_principal_pausa_reabre_e_so_depois_persiste(self):
        from apps.scrapers import ml_conexao

        page = self._page(self.CAMERA_URL)
        context = self._contexto(page)
        abriu = []
        persistiu = []
        storage_antes_do_retry = []
        retry_enviado = threading.Event()

        def abrir_login(_page):
            abriu.append(1)
            page.url = self.CAMERA_URL if len(abriu) == 1 else "https://www.mercadolivre.com.br/"

        original_storage = context.storage_state

        def storage_state():
            if not retry_enviado.is_set():
                storage_antes_do_retry.append(True)
            return original_storage.return_value

        context.storage_state.side_effect = storage_state
        observado = []

        def responder():
            self._responder_ao_guia(
                ml_conexao._cache_key(self.user.id),
                lambda **kw: (retry_enviado.set(), ml_conexao._set_estado(self.user.id, **kw)),
                observado, "login",
            )

        thread = threading.Thread(target=responder, daemon=True)
        thread.start()
        with patch("playwright.sync_api.sync_playwright", self._playwright_falso(page, context)), \
             patch.object(ml_conexao, "_ir_para_login", side_effect=abrir_login), \
             patch("apps.scrapers.conexoes.sondar_sessao_ml", return_value=("conectado", "")), \
             patch.object(ml_conexao, "_validar_linkbuilder_ao_vivo", return_value=("ready", "ok")), \
             patch.object(ml_conexao, "_persistir_sessao", side_effect=lambda *a, **k: persistiu.append(1)):
            ml_conexao._worker(self.user.id)
        thread.join(timeout=1)

        self.assertTrue(observado)
        self.assertEqual(len(abriu), 2)
        self.assertFalse(storage_antes_do_retry)
        self.assertEqual(persistiu, [1])
        self.assertEqual(cache.get(ml_conexao._cache_key(self.user.id))["fase"], "conectado")

    def test_linkbuilder_pausa_e_retorna_ao_destino_correto(self):
        from apps.scrapers import ml_conexao
        from apps.scrapers.ml_live_transport import ActivePage
        from apps.scrapers.scraper_mercadolivre import link

        page = self._page(self.CAMERA_URL)
        destinos = []

        def goto(url, **_kw):
            destinos.append(url)
            page.url = self.CAMERA_URL if len(destinos) == 1 else url

        page.goto.side_effect = goto
        context = self._contexto(page)
        runtime = ml_conexao._transport.create(self.user.id)
        self.addCleanup(ml_conexao._transport.finish, self.user.id, runtime)
        active_page = ActivePage(context, page, runtime)
        ml_conexao._set_estado(self.user.id, fase="validando_linkbuilder", desafio={})
        observado = []

        responder = threading.Thread(
            target=self._responder_ao_guia,
            args=(
                ml_conexao._cache_key(self.user.id),
                lambda **kw: ml_conexao._set_estado(self.user.id, **kw),
                observado, "linkbuilder",
            ),
            daemon=True,
        )
        responder.start()
        with patch.object(link, "_linkbuilder_pronto", side_effect=lambda p: p.url == link._LB_URL), \
             patch.object(link, "_pagina_de_login", return_value=False), \
             patch.object(link, "_pagina_intersticial", return_value=False):
            result = ml_conexao._validar_linkbuilder_ao_vivo(
                self.user.id, active_page, runtime, context, time.time() + 3,
            )
        responder.join(timeout=1)

        self.assertTrue(observado)
        self.assertEqual(result[0], "ready")
        page.goto.assert_called_with(
            link._LB_URL, wait_until="domcontentloaded", timeout=link._LB_TIMEOUT_MS,
        )

    def test_relatorios_pausam_reabrem_e_persistem(self):
        from apps.scrapers import ml_relatorio_conexao

        page = self._page(self.CAMERA_URL)
        chamadas_goto = []

        def goto(url, **_kwargs):
            chamadas_goto.append(url)
            page.url = self.CAMERA_URL if len(chamadas_goto) == 1 else ml_relatorio_conexao._report_url()

        page.goto.side_effect = goto
        context = self._contexto(page)
        context.pages = []
        context.new_page.return_value = page
        salvo = []
        observado = []
        responder = threading.Thread(
            target=self._responder_ao_guia,
            args=(
                ml_relatorio_conexao._key(self.user.id),
                lambda **kw: ml_relatorio_conexao._set(self.user.id, **kw),
                observado, "relatorios",
            ),
            daemon=True,
        )
        responder.start()
        with patch("playwright.sync_api.sync_playwright", self._playwright_falso(page, context)), \
             patch.object(ml_relatorio_conexao, "save_report_state", side_effect=lambda *a: salvo.append(1)):
            ml_relatorio_conexao._worker(self.user)
        responder.join(timeout=1)

        self.assertTrue(observado)
        self.assertEqual(len(chamadas_goto), 2)
        self.assertEqual(salvo, [1])
        self.assertEqual(cache.get(ml_relatorio_conexao._key(self.user.id))["fase"], "conectado")


class EndpointsDoOnboardingQRTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username=f"qr-http-{time.time_ns()}", password="test",
        )
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)

    def test_endpoint_principal_repassa_apenas_usuario_autenticado(self):
        payload = {"ok": True, "fase": "configurar_qr"}
        with patch(
            "apps.scrapers.ml_conexao.retentar_apos_configurar_qr",
            return_value=(True, payload),
        ) as retry:
            response = self.client.post(
                reverse("scraper-ml-qr-retry"),
                data=json.dumps({"url": "https://malicioso.invalid"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        retry.assert_called_once_with(self.user.id)

    def test_endpoint_de_relatorios_usa_o_mesmo_contrato(self):
        with patch(
            "apps.scrapers.ml_relatorio_conexao.retentar_apos_configurar_qr",
            return_value=(True, {"ok": True, "fase": "configurar_qr"}),
        ) as retry:
            response = self.client.post(reverse("scraper-ml-relatorio-qr-retry"))
        self.assertEqual(response.status_code, 200)
        retry.assert_called_once_with(self.user.id)

    def test_fase_invalida_retorna_409_sem_detalhe_interno(self):
        with patch(
            "apps.scrapers.ml_conexao.retentar_apos_configurar_qr",
            return_value=(False, {"ok": False, "erro": "O login não está aguardando o QR."}),
        ):
            response = self.client.post(reverse("scraper-ml-qr-retry"))
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("traceback", response.content.decode().lower())

    def test_get_e_rejeitado(self):
        self.assertEqual(self.client.get(reverse("scraper-ml-qr-retry")).status_code, 405)

    def test_anonimo_nao_acessa(self):
        response = Client().post(reverse("scraper-ml-qr-retry"))
        self.assertEqual(response.status_code, 302)

    def test_csrf_e_obrigatorio(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        response = strict.post(reverse("scraper-ml-qr-retry"))
        self.assertEqual(response.status_code, 403)


class TemplateDoOnboardingQRTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username=f"qr-template-{time.time_ns()}", password="test",
        )
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)
        template = Path(settings.BASE_DIR) / "apps/templates/scrapers/ml_conexao.html"
        self.html = template.read_text(encoding="utf-8")

    def test_contrato_js_fecha_sse_e_reabre_pelo_prefixo(self):
        self.assertIn("configurar_qr", self.html)
        self.assertIn("`${PREFIX}/qr/retry/`", self.html)
        self.assertIn("show(liveArea, connecting, 'grid')", self.html)
        self.assertIn("watchFrames(connecting)", self.html)

    def test_nao_tenta_capturar_biometria(self):
        self.assertNotIn("getUserMedia", self.html)
        self.assertNotIn("camera=", self.html)

    def test_ajuda_oficial_e_alvos_de_toque(self):
        self.assertIn("https://www.mercadolivre.com.br/ajuda/31030", self.html)
        self.assertIn("min-height:44px", self.html)

    def test_falha_de_rede_nao_expoe_mensagem_tecnica_do_navegador(self):
        self.assertIn("Não foi possível falar com o servidor", self.html)
        self.assertNotIn("qrActionMsg.textContent = error.message", self.html)

    def test_painel_renderiza_no_ml_e_nao_na_amazon(self):
        with patch("apps.scrapers.ml_conexao.status", return_value={"fase": "idle"}):
            ml = self.client.get(reverse("scraper-ml-conexao"))
        with patch("apps.scrapers.amazon_conexao.status", return_value={"fase": "idle"}):
            amazon = self.client.get(reverse("scraper-amazon-conexao"))
        self.assertContains(ml, 'id="qr-setup-area"')
        self.assertNotContains(amazon, 'id="qr-setup-area"')
