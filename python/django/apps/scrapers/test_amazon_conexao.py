"""Live view da conexão Amazon Associados.

O transporte legado (CDP Page.startScreencast) só emite quadro quando há mudança
visual. Medido em homologação: uma página de login parada produz ZERO frames em
40s. Isso disparava o watchdog do cliente (overlay "Reconectando" por cima do
CAPTCHA) e o corte do gerador, que matava os listeners de teclado.
"""
from django.test import TestCase

from apps.scrapers import amazon_conexao


class LogadoResilienteTests(TestCase):
    class _PageQueDerruba:
        url = "https://associados.amazon.com.br/home/reports"

        def locator(self, _seletor):
            raise RuntimeError(
                "Execution context was destroyed, most likely because of a navigation")

    class _PageLogada:
        url = "https://associados.amazon.com.br/home/reports"

        def locator(self, _seletor):
            return type("L", (), {"count": staticmethod(lambda: 0)})()

    def test_excecao_de_navegacao_nao_derruba_o_worker(self):
        self.assertFalse(amazon_conexao._logado(self._PageQueDerruba()))

    def test_pagina_autenticada_sem_campo_de_senha_conta_como_logada(self):
        self.assertTrue(amazon_conexao._logado(self._PageLogada()))

    def test_url_de_signin_nunca_conta_como_logada(self):
        page = self._PageLogada()
        page.url = "https://www.amazon.com.br/ap/signin?openid.mode=checkid_setup"
        self.assertFalse(amazon_conexao._logado(page))


class TransporteAmazonTests(TestCase):
    """A Amazon usa o MESMO transporte do ML: heartbeat, ACK e sessão."""

    def setUp(self):
        self.uid = 4242
        self.runtime = amazon_conexao._transport.create(self.uid)
        self.addCleanup(amazon_conexao._transport.finish, self.uid, self.runtime)

    def test_status_expoe_sessao_viewport_e_stream(self):
        estado = amazon_conexao._transport.status(self.uid)
        self.assertEqual(estado["session_id"], self.runtime.session_id)
        self.assertIn("viewport", estado)
        self.assertIn("stream", estado)

    def test_input_exige_a_sessao_corrente(self):
        recusado = amazon_conexao.enfileirar_input(
            self.uid, "sessao-antiga", [{"seq": 1, "t": "char", "text": "s"}])
        self.assertFalse(recusado["ok"])
        self.assertEqual(recusado["erro"], "sessao_desatualizada")
        self.assertTrue(self.runtime.input_queue.empty())

    def test_input_valido_e_confirmado_com_ack(self):
        aceito = amazon_conexao.enfileirar_input(
            self.uid, self.runtime.session_id,
            [{"seq": 1, "t": "char", "text": "a"}])
        self.assertTrue(aceito["ok"])
        self.assertEqual(aceito["ack"], 1)

    def test_evento_rejeitado_nao_congela_a_digitacao(self):
        """O mesmo deadlock que travava a tela do ML não pode existir aqui."""
        resultado = amazon_conexao.enfileirar_input(
            self.uid, self.runtime.session_id, [
                {"seq": 1, "t": "char", "text": "a"},
                {"seq": 2, "t": "move", "x": None, "y": 10},
                {"seq": 3, "t": "char", "text": "b"},
            ])
        self.assertEqual(resultado["ack"], 3)


class TemplateLiveViewTests(TestCase):
    """Tela parada (CAPTCHA sendo lido) não pode acusar queda sozinha."""

    def setUp(self):
        with open("apps/templates/scrapers/ml_conexao.html", encoding="utf-8") as fh:
            self.html = fh.read()

    def test_watchdog_tolera_mais_que_o_intervalo_de_heartbeat(self):
        # 4000 era menor que os 10s de heartbeat do servidor: acusava queda sozinho.
        self.assertIn("lastFrameAt > 12000", self.html)
        self.assertNotIn("lastFrameAt > 4000", self.html)

    def test_canvas_so_e_redimensionado_quando_muda(self):
        # Reatribuir width/height limpa o canvas — era a origem do "piscando".
        self.assertIn(
            "if (canvas.width === viewport.width && canvas.height === viewport.height) return;",
            self.html,
        )
