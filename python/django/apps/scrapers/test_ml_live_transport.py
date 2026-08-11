import json
import queue
import threading
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from apps.scrapers.ml_live_transport import (
    ACTIVE_WINDOW_S,
    BURST_WINDOW_S,
    CAPTURE_ACTIVE_INTERVAL_S,
    CAPTURE_BURST_INTERVAL_S,
    CAPTURE_IDLE_INTERVAL_S,
    CAPTURE_QUALITY,
    ActivePage,
    InteractiveBrowserCapacityError,
    LiveTransport,
    despachar_input,
    interactive_browser_slot,
    intervalo_de_captura,
    normalizar_viewport,
)


class InteractiveBrowserCapacityTests(SimpleTestCase):
    def test_slot_compartilhado_rejeita_segundo_chromium_e_e_liberado(self):
        from apps.scrapers import ml_live_transport

        semaphore = threading.BoundedSemaphore(1)
        with patch.object(ml_live_transport, "_interactive_browser_slots", semaphore):
            with interactive_browser_slot():
                with self.assertRaises(InteractiveBrowserCapacityError):
                    with interactive_browser_slot():
                        self.fail("um segundo Chromium interativo não poderia abrir")
            # A saída, inclusive após a recusa concorrente, devolve o slot.
            with interactive_browser_slot():
                pass


class ViewportRemotoTests(SimpleTestCase):
    def test_mobile_preserva_formato_retrato_e_limita_valores(self):
        viewport = normalizar_viewport({
            "viewport": {"width": 320, "height": 1200},
            "device_pixel_ratio": 9,
            "pointer": "coarse",
        })
        self.assertEqual(viewport["width"], 360)
        self.assertEqual(viewport["height"], 932)
        self.assertEqual(viewport["device_pixel_ratio"], 2)
        self.assertEqual(viewport["device_class"], "mobile")

    def test_desktop_preserva_default_legado(self):
        viewport = normalizar_viewport(None)
        self.assertEqual(
            (viewport["width"], viewport["height"]),
            (1280, 800),
        )
        self.assertEqual(viewport["pointer"], "fine")

    def test_payload_invalido_nao_escapa_dos_limites(self):
        viewport = normalizar_viewport({
            "viewport": {"width": "<script>", "height": object()},
            "device_pixel_ratio": "nan",
            "pointer": "inventado",
        })
        self.assertGreaterEqual(viewport["width"], 1024)
        self.assertLessEqual(viewport["height"], 1000)
        self.assertIn(viewport["pointer"], {"coarse", "fine"})


class TransporteEntradaTests(SimpleTestCase):
    def setUp(self):
        self.transport = LiveTransport("test")
        self.runtime = self.transport.create(
            10, {"viewport": {"width": 390, "height": 844}},
        )

    def tearDown(self):
        self.transport.finish(10, self.runtime)

    def test_confirma_e_deduplica_lote_repetido(self):
        events = [
            {"seq": 1, "t": "char", "text": "a"},
            {"seq": 2, "t": "key", "key": "Enter"},
        ]
        first = self.transport.enqueue(10, self.runtime.session_id, events)
        repeated = self.transport.enqueue(10, self.runtime.session_id, events)
        self.assertEqual(first, {"ok": True, "aceitos": 2, "ack": 2})
        self.assertEqual(repeated, {"ok": True, "aceitos": 0, "ack": 2})
        self.assertEqual(self.runtime.input_queue.qsize(), 2)
        self.assertEqual(self.runtime.public_state()["stream"]["input_retries"], 1)

    def test_ordena_lote_e_ack_so_avanca_sem_lacuna(self):
        ordered = self.transport.enqueue(10, self.runtime.session_id, [
            {"seq": 2, "t": "char", "text": "b"},
            {"seq": 1, "t": "char", "text": "a"},
        ])
        self.assertEqual(ordered["ack"], 2)
        self.assertEqual(
            [self.runtime.input_queue.get_nowait()["text"] for _ in range(2)],
            ["a", "b"],
        )

        gap = self.transport.enqueue(
            10, self.runtime.session_id,
            [{"seq": 4, "t": "char", "text": "d"}],
        )
        self.assertEqual(gap["ack"], 2)
        self.assertEqual(gap["aceitos"], 0)

    def test_evento_rejeitado_nao_congela_o_ack(self):
        """Um evento descartado consome o seq; só lacuna real segura a faixa.

        Coordenada nula chega quando o canvas está oculto/em transição. Antes o
        ack parava nesse buraco para sempre e o cliente reenviava o buffer
        indefinidamente — a digitação travava sem erro visível.
        """
        result = self.transport.enqueue(10, self.runtime.session_id, [
            {"seq": 1, "t": "char", "text": "a"},
            {"seq": 2, "t": "move", "x": None, "y": 10},
            {"seq": 3, "t": "char", "text": "b"},
        ])

        self.assertEqual(result["ack"], 3)
        self.assertEqual(result["aceitos"], 2)
        self.assertEqual(
            [self.runtime.input_queue.get_nowait()["text"] for _ in range(2)],
            ["a", "b"],
        )

    def test_digitacao_continua_apos_evento_rejeitado(self):
        self.transport.enqueue(10, self.runtime.session_id, [
            {"seq": 1, "t": "key", "key": "TeclaInexistente"},
        ])
        seguinte = self.transport.enqueue(10, self.runtime.session_id, [
            {"seq": 2, "t": "char", "text": "x"},
        ])
        self.assertEqual(seguinte["ack"], 2)
        self.assertEqual(seguinte["aceitos"], 1)

    def test_sessao_antiga_nao_atinge_login_novo(self):
        result = self.transport.enqueue(
            10, "outra-sessao", [{"seq": 1, "t": "char", "text": "segredo"}],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["erro"], "sessao_desatualizada")
        self.assertTrue(self.runtime.input_queue.empty())

    def test_sessoes_de_usuarios_nao_compartilham_filas(self):
        other = self.transport.create(
            11, {"viewport": {"width": 1440, "height": 900}},
        )
        try:
            rejected = self.transport.enqueue(
                11, self.runtime.session_id,
                [{"seq": 1, "t": "char", "text": "isolado"}],
            )
            accepted = self.transport.enqueue(
                10, self.runtime.session_id,
                [{"seq": 1, "t": "char", "text": "correto"}],
            )
            self.assertEqual(rejected["erro"], "sessao_desatualizada")
            self.assertEqual(accepted["ack"], 1)
            self.assertTrue(other.input_queue.empty())
        finally:
            self.transport.finish(11, other)

    def test_coordenadas_e_texto_sao_limitados(self):
        result = self.transport.enqueue(10, self.runtime.session_id, [
            {"seq": 1, "t": "down", "x": 9999, "y": -4, "button": "unknown"},
            {"seq": 2, "t": "char", "text": "x" * 100},
        ])
        self.assertEqual(result["ack"], 2)
        click = self.runtime.input_queue.get_nowait()
        text = self.runtime.input_queue.get_nowait()
        self.assertEqual((click["x"], click["y"], click["button"]), (390, 0, "left"))
        self.assertEqual(len(text["text"]), 16)

    def test_fila_cheia_retorna_ultimo_ack_realmente_aceito(self):
        self.runtime.input_queue = queue.Queue(maxsize=1)
        result = self.transport.enqueue(10, self.runtime.session_id, [
            {"seq": 1, "t": "char", "text": "a"},
            {"seq": 2, "t": "char", "text": "b"},
        ])
        self.assertEqual(result["ack"], 1)
        self.assertEqual(result["aceitos"], 1)

    def test_log_nunca_contem_conteudo_digitado(self):
        page = Mock()
        page.keyboard.type.side_effect = RuntimeError("falhou")
        secret = "senha-que-nao-pode-aparecer"
        with self.assertLogs("apps.scrapers.ml_live_transport", level="DEBUG") as logs:
            despachar_input(page, {"t": "char", "text": secret})
        self.assertNotIn(secret, "\n".join(logs.output))


class CadenciaDeCapturaTests(SimpleTestCase):
    """A cadência é o que decidia se a tela era usável.

    Com 250ms fixos cada tecla levava quase meio segundo para aparecer e captcha de
    arrastar era impossível de resolver. A rajada precisa valer durante a interação e
    precisa MESMO acabar depois dela, senão 14 FPS contínuos disputam a CPU com os
    workers de automação na mesma máquina.
    """

    def setUp(self):
        self.transport = LiveTransport("cadencia")
        self.runtime = self.transport.create(30)

    def tearDown(self):
        self.transport.finish(30, self.runtime)

    def test_input_recente_libera_rajada(self):
        self.runtime.last_input_at = 1000.0
        self.assertEqual(
            intervalo_de_captura(self.runtime, now=1000.5, active=False),
            CAPTURE_BURST_INTERVAL_S,
        )

    def test_passada_a_rajada_volta_para_a_cadencia_ativa(self):
        self.runtime.last_input_at = 1000.0
        self.assertEqual(
            intervalo_de_captura(
                self.runtime, now=1000.0 + BURST_WINDOW_S + 0.1, active=False,
            ),
            CAPTURE_ACTIVE_INTERVAL_S,
        )

    def test_usuario_parado_cai_para_a_cadencia_ociosa(self):
        self.runtime.last_input_at = 1000.0
        self.assertEqual(
            intervalo_de_captura(
                self.runtime, now=1000.0 + ACTIVE_WINDOW_S + 0.1, active=False,
            ),
            CAPTURE_IDLE_INTERVAL_S,
        )

    def test_sessao_sem_nenhum_input_nao_nasce_em_rajada(self):
        # `last_input_at` zerado é uma sessão recém-aberta, não input no instante 0.
        self.assertEqual(
            intervalo_de_captura(self.runtime, now=5.0, active=False),
            CAPTURE_IDLE_INTERVAL_S,
        )

    def test_evento_aceito_abre_a_janela_de_rajada(self):
        self.assertEqual(self.runtime.last_input_at, 0.0)
        self.transport.enqueue(
            30, self.runtime.session_id, [{"seq": 1, "t": "char", "text": "a"}],
        )
        self.assertGreater(self.runtime.last_input_at, 0.0)

    def test_lote_todo_rejeitado_nao_abre_rajada(self):
        self.transport.enqueue(30, "sessao-errada", [{"seq": 1, "t": "char", "text": "a"}])
        self.assertEqual(self.runtime.last_input_at, 0.0)


class CapturaEPopupTests(SimpleTestCase):
    def test_captura_publica_frame_numerado(self):
        transport = LiveTransport("capture")
        runtime = transport.create(1)
        page = Mock()
        page.screenshot.return_value = b"jpeg"
        self.assertTrue(transport.capture(runtime, page, active=True))
        page.screenshot.assert_called_once_with(
            type="jpeg", quality=CAPTURE_QUALITY, scale="css",
        )
        event = next(transport.frames(1, runtime.session_id))
        self.assertEqual(event["event"], "frame")
        self.assertEqual(event["id"], 1)
        stream = runtime.public_state()["stream"]
        self.assertIsNotNone(stream["first_frame_ms"])
        self.assertGreaterEqual(stream["frame_age_ms"], 0)
        transport.finish(1, runtime)

    def test_reconexao_do_stream_recebe_somente_o_frame_atual(self):
        transport = LiveTransport("reconnect")
        runtime = transport.create(1)
        page = Mock()
        page.screenshot.side_effect = [b"primeiro", b"segundo"]
        transport.capture(runtime, page, active=True)
        first_stream = transport.frames(1, runtime.session_id)
        self.assertEqual(next(first_stream)["id"], 1)

        runtime.last_capture_at = 0
        transport.capture(runtime, page, active=True)
        reconnected = transport.frames(1, runtime.session_id)
        current = next(reconnected)
        self.assertEqual(current["id"], 2)
        self.assertNotEqual(current["data"], "")
        first_stream.close()
        reconnected.close()
        transport.finish(1, runtime)

    def test_tela_identica_nao_vira_quadro_novo_mas_segue_viva(self):
        transport = LiveTransport("dedupe")
        runtime = transport.create(1)
        page = Mock()
        page.screenshot.return_value = b"mesma-tela"
        transport.capture(runtime, page, active=True)
        runtime.last_capture_at = 0
        transport.capture(runtime, page, active=True)
        self.assertEqual(runtime.frame_seq, 1)
        self.assertEqual(runtime.public_state()["stream"]["estado"], "ao_vivo")
        transport.finish(1, runtime)

    def test_popup_vira_pagina_ativa_e_fecha_com_fallback(self):
        callbacks = {}
        context = Mock()
        context.on.side_effect = lambda name, callback: callbacks.setdefault(name, callback)
        initial = Mock()
        initial.is_closed.return_value = False
        runtime = LiveTransport("popup").create(2)
        pages = ActivePage(context, initial, runtime)

        popup = Mock()
        popup.is_closed.return_value = False
        callbacks["page"](popup)
        self.assertIs(pages.current(), popup)
        self.assertEqual(runtime.stream_state, "reconectando")

        popup.is_closed.return_value = True
        self.assertIs(pages.current(), initial)


class ValidacaoRelatorioTests(SimpleTestCase):
    @patch(
        "apps.scrapers.ml_relatorio_conexao._report_url",
        return_value="https://www.mercadolivre.com.br/afiliados/linkbuilder#hub",
    )
    def test_rota_intermediaria_nunca_e_sucesso(self, _report_url):
        from apps.scrapers.ml_relatorio_conexao import _logado

        page = Mock()
        page.url = "https://www.mercadolivre.com.br/ato-complaint/classifier"
        self.assertFalse(_logado(page))

    @patch(
        "apps.scrapers.ml_relatorio_conexao._report_url",
        return_value="https://www.mercadolivre.com.br/afiliados/linkbuilder#hub",
    )
    def test_destino_autenticado_sem_senha_e_sucesso(self, _report_url):
        from apps.scrapers.ml_relatorio_conexao import _logado

        page = Mock()
        page.url = "https://www.mercadolivre.com.br/afiliados/linkbuilder#hub"
        page.locator.return_value.count.return_value = 0
        self.assertTrue(_logado(page))


class ValidacaoAmazonTests(SimpleTestCase):
    """A landing pública da Amazon vive no mesmo domínio das rotas autenticadas.

    Aceitá-la como login concluído gravava uma sessão de cookies anônimos, e o sync de
    comissão depois falhava sem explicação.
    """

    def test_landing_publica_nao_conta_como_login(self):
        from apps.scrapers.amazon_conexao import _logado

        page = Mock()
        page.url = "https://associados.amazon.com.br/"
        page.locator.return_value.count.return_value = 0
        self.assertFalse(_logado(page))

    def test_pagina_de_signin_nao_conta_como_login(self):
        from apps.scrapers.amazon_conexao import _logado

        page = Mock()
        page.url = "https://www.amazon.com.br/ap/signin?openid.return_to=/home"
        page.locator.return_value.count.return_value = 0
        self.assertFalse(_logado(page))

    def test_relatorio_sem_campo_de_senha_e_sucesso(self):
        from apps.scrapers.amazon_conexao import _logado

        page = Mock()
        page.url = "https://associados.amazon.com.br/home/reports"
        page.locator.return_value.count.return_value = 0
        self.assertTrue(_logado(page))


class PaginaDeErroDoMLTests(SimpleTestCase):
    """Sem detectar a página de erro do ML, o worker esperava um login já recusado
    até o deadline de 10 minutos e o usuário não tinha nenhuma pista do motivo."""

    def _page(self, texto, *, campos=0):
        page = Mock()
        page.evaluate.return_value = texto.lower()
        page.locator.return_value.count.return_value = campos
        return page

    def test_reconhece_a_mensagem_do_gateway(self):
        from apps.scrapers.ml_conexao import _pagina_de_erro_do_ml

        page = self._page("Ops! Ocorreu um erro. Tente novamente mais tarde.")
        self.assertTrue(_pagina_de_erro_do_ml(page))

    def test_tela_de_login_com_formulario_nao_e_bloqueio(self):
        from apps.scrapers.ml_conexao import _pagina_de_erro_do_ml

        # Páginas de login embarcam mensagens de validação no DOM. Um campo de senha
        # presente significa que ainda dá para digitar — não é o gateway recusando.
        page = self._page(
            "Ocorreu um erro. Tente novamente mais tarde. Informe sua senha",
            campos=1,
        )
        self.assertFalse(_pagina_de_erro_do_ml(page))

    def test_login_normal_nao_dispara_alerta(self):
        from apps.scrapers.ml_conexao import _pagina_de_erro_do_ml

        page = self._page("Mercado Livre — Entre na sua conta", campos=2)
        self.assertFalse(_pagina_de_erro_do_ml(page))

    def test_falha_ao_ler_a_pagina_nunca_vira_falso_positivo(self):
        from apps.scrapers.ml_conexao import _pagina_de_erro_do_ml

        page = Mock()
        page.evaluate.side_effect = RuntimeError("página navegando")
        self.assertFalse(_pagina_de_erro_do_ml(page))


class FingerprintDoLoginTests(SimpleTestCase):
    """O UA sorteado era a causa do "Ops! Ocorreu um erro": metade do pool não é
    Chrome, e o ML compara o UA com o que o runtime responde depois."""

    def test_nenhum_worker_de_login_sorteia_user_agent(self):
        import inspect

        from apps.scrapers import amazon_conexao, ml_conexao, ml_relatorio_conexao

        for modulo in (ml_conexao, ml_relatorio_conexao, amazon_conexao):
            self.assertNotIn(
                "ua_aleatorio", inspect.getsource(modulo),
                f"{modulo.__name__} voltou a rotacionar user agent no login",
            )

    def test_contexto_declara_locale_e_fuso_brasileiros(self):
        from apps.scrapers.contexto_login import opcoes_de_contexto

        browser = Mock()
        browser.new_browser_cdp_session.return_value.send.return_value = {
            "userAgent": "Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/140.0.0.0",
        }
        opcoes = opcoes_de_contexto(browser, normalizar_viewport(None))
        self.assertEqual(opcoes["locale"], "pt-BR")
        self.assertEqual(opcoes["timezone_id"], "America/Sao_Paulo")
        self.assertIn("pt-BR", opcoes["extra_http_headers"]["Accept-Language"])

    def test_user_agent_vem_do_binario_sem_marcador_de_headless(self):
        from apps.scrapers.contexto_login import opcoes_de_contexto

        browser = Mock()
        browser.new_browser_cdp_session.return_value.send.return_value = {
            "userAgent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like "
                "Gecko) HeadlessChrome/140.0.0.0 Safari/537.36"
            ),
        }
        opcoes = opcoes_de_contexto(browser, normalizar_viewport(None))
        self.assertNotIn("HeadlessChrome", opcoes["user_agent"])
        self.assertIn("Chrome/140.0.0.0", opcoes["user_agent"])

    def test_leitura_falha_nao_inventa_user_agent(self):
        # Um UA default com "HeadlessChrome" é um sinal ruim; um UA falso e
        # contraditório com o runtime é pior. Na dúvida, não sobrescreve.
        from apps.scrapers.contexto_login import opcoes_de_contexto

        browser = Mock()
        browser.new_browser_cdp_session.side_effect = RuntimeError("sem CDP")
        self.assertNotIn(
            "user_agent", opcoes_de_contexto(browser, normalizar_viewport(None)),
        )


class ContratoHTTPLoginMLTests(TestCase):
    def setUp(self):
        from apps.accounts.models import ensure_personal_organization

        self.user = get_user_model().objects.create_user("ml-http", password="test")
        self.user.perfil.marcar_verificado()
        ensure_personal_organization(self.user)
        self.client.force_login(self.user)

    @patch("apps.scrapers.ml_conexao.criar_sessao")
    def test_start_repassa_configuracao_do_cliente(self, create):
        create.return_value = {
            "fase": "iniciando", "session_id": "abc",
            "viewport": {"width": 390, "height": 844},
        }
        payload = {
            "viewport": {"width": 390, "height": 844},
            "device_pixel_ratio": 3,
            "pointer": "coarse",
        }
        response = self.client.post(
            reverse("scraper-ml-start"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        create.assert_called_once_with(self.user, payload)

    @patch("apps.scrapers.ml_relatorio_conexao.criar_sessao")
    def test_start_de_relatorios_usa_o_mesmo_contrato(self, create):
        create.return_value = {
            "fase": "iniciando", "session_id": "report",
            "viewport": {"width": 1440, "height": 900},
        }
        payload = {
            "viewport": {"width": 1440, "height": 900},
            "device_pixel_ratio": 1,
            "pointer": "fine",
        }
        response = self.client.post(
            reverse("scraper-ml-relatorio-start"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        create.assert_called_once_with(self.user, payload)

    def test_start_recusa_json_invalido_e_payload_grande(self):
        invalid = self.client.post(
            reverse("scraper-ml-start"), data="{", content_type="application/json",
        )
        huge = self.client.post(
            reverse("scraper-ml-start"), data="x" * 5000,
            content_type="application/json",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(huge.status_code, 413)

    def test_input_recusa_sessao_inexistente_sem_vazar_dados(self):
        response = self.client.post(
            reverse("scraper-ml-input"),
            data=json.dumps({
                "session_id": "inexistente",
                "events": [{"seq": 1, "t": "char", "text": "<script>alert(1)</script>"}],
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["erro"], "sessao_inativa")
        self.assertNotContains(response, "<script>", status_code=200)

    def test_input_recusa_json_invalido_e_payload_grande(self):
        invalid = self.client.post(
            reverse("scraper-ml-input"), data="{", content_type="application/json",
        )
        huge = self.client.post(
            reverse("scraper-ml-input"), data="x" * 70000,
            content_type="application/json",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(huge.status_code, 413)

    def test_input_exige_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        response = csrf_client.post(
            reverse("scraper-ml-input"),
            data=json.dumps({"session_id": "x", "events": []}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_template_usa_textcontent_e_nao_innerhtml_para_erros(self):
        response = self.client.get(reverse("scraper-ml-conexao"))
        self.assertContains(response, "errText.textContent = state.erro")
        self.assertNotContains(response, "errBox.innerHTML")
        self.assertContains(response, "pointercancel")
        self.assertContains(response, "Verificar conexão")

    def test_template_nao_reatribui_o_canvas_a_cada_poll(self):
        # Atribuir canvas.width APAGA o bitmap. Como paint() roda a cada poll e sempre
        # repassava o viewport, a imagem sumia a cada 3 segundos: era o "tela piscando".
        response = self.client.get(reverse("scraper-ml-conexao"))
        self.assertContains(
            response,
            "if (canvas.width === viewport.width && "
            "canvas.height === viewport.height) return;",
            html=False,
        )

    def test_template_nao_atrasa_o_proximo_lote_de_input_em_sucesso(self):
        # `setTimeout(flush, retryDelay)` em sucesso fazia cada tecla esperar o POST
        # anterior mais 250ms, travando a digitação em ~3 caracteres por segundo.
        response = self.client.get(reverse("scraper-ml-conexao"))
        self.assertContains(response, "entregou ? 0 : retryDelay")

    def test_template_tem_um_unico_transporte(self):
        # O ramo legado (screencast da Amazon, sem seq/ACK) saiu junto com a migração.
        response = self.client.get(reverse("scraper-ml-conexao"))
        self.assertNotContains(response, "LIVE_V2")
        self.assertNotContains(response, "__DONE__")


class ContratoHTTPLoginAmazonTests(TestCase):
    """A Amazon usava um contrato próprio: sem session_id, sem seq/ACK, frames sem
    numeração. Depois da migração ela responde ao mesmo protocolo do ML."""

    def setUp(self):
        from apps.accounts.models import ensure_personal_organization

        self.user = get_user_model().objects.create_user("amz-http", password="test")
        self.user.perfil.marcar_verificado()
        ensure_personal_organization(self.user)
        self.client.force_login(self.user)

    @patch("apps.scrapers.amazon_conexao.criar_sessao")
    def test_start_repassa_configuracao_do_cliente(self, create):
        create.return_value = {
            "fase": "iniciando", "session_id": "amz",
            "viewport": {"width": 390, "height": 844},
        }
        payload = {
            "viewport": {"width": 390, "height": 844},
            "device_pixel_ratio": 3,
            "pointer": "coarse",
        }
        response = self.client.post(
            reverse("scraper-amazon-start"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        create.assert_called_once_with(self.user, payload)

    def test_input_exige_sessao_valida(self):
        response = self.client.post(
            reverse("scraper-amazon-input"),
            data=json.dumps({
                "session_id": "inexistente",
                "events": [{"seq": 1, "t": "char", "text": "senha"}],
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["erro"], "sessao_inativa")

    def test_input_recusa_json_invalido_e_payload_grande(self):
        invalid = self.client.post(
            reverse("scraper-amazon-input"), data="{",
            content_type="application/json",
        )
        huge = self.client.post(
            reverse("scraper-amazon-input"), data="x" * 70000,
            content_type="application/json",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(huge.status_code, 413)

    def test_tela_da_amazon_mantem_os_proprios_textos(self):
        # `live_v2` fazia dois trabalhos: escolher o transporte e escolher os textos.
        # Com a Amazon também no transporte novo, o flag de texto virou explícito
        # (`marketplace_ml`) — senão a tela da Amazon passaria a dizer "Mercado Livre"
        # em todo lugar, porque as duas passaram a ter o mesmo transporte.
        response = self.client.get(reverse("scraper-amazon-conexao"))
        self.assertContains(response, "Amazon Associados")
        self.assertContains(response, "Abrir a Amazon")
        self.assertContains(response, "Já entrei")
        self.assertNotContains(response, "Abrir o Mercado Livre")
        self.assertNotContains(response, "Verificar conexão")

    @patch("apps.accounts.feature_flags.enabled_for_user", return_value=False)
    def test_flag_desligada_persiste_indisponivel(self, _flag):
        # Sem persistir no cache, o poll seguinte lia a fase antiga + auth_valido e
        # repintava "Conectado" sobre um login que nunca abriu.
        from apps.scrapers import amazon_conexao

        estado = amazon_conexao.criar_sessao(self.user, None)
        self.assertEqual(estado["fase"], "indisponivel")
        self.assertFalse(estado["auth_valido"])
        self.assertEqual(amazon_conexao.status(self.user.id)["fase"], "indisponivel")
