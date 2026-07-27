import json
import queue
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from apps.scrapers.ml_live_transport import (
    ActivePage,
    LiveTransport,
    despachar_input,
    normalizar_viewport,
)


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


class CapturaEPopupTests(SimpleTestCase):
    def test_captura_publica_frame_numerado(self):
        transport = LiveTransport("capture")
        runtime = transport.create(1)
        page = Mock()
        page.screenshot.return_value = b"jpeg"
        self.assertTrue(transport.capture(runtime, page, active=True))
        page.screenshot.assert_called_once_with(
            type="jpeg", quality=78, scale="css",
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
