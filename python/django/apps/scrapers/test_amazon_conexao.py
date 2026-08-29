"""Live view da conexão Amazon Associados.

O transporte legado (CDP Page.startScreencast) só emite quadro quando há mudança
visual. Medido em homologação: uma página de login parada produz ZERO frames em
40s. Isso disparava o watchdog do cliente (overlay "Reconectando" por cima do
CAPTCHA) e o corte do gerador, que matava os listeners de teclado.
"""
from pathlib import Path

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

    def test_conta_de_compras_exige_rota_interna_da_loja(self):
        page = self._PageLogada()
        page.url = "https://www.amazon.com.br/gp/css/homepage.html"
        self.assertTrue(amazon_conexao._logado(page, shopper=True))
        page.url = "https://www.amazon.com.br/"
        self.assertFalse(amazon_conexao._logado(page, shopper=True))


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


class AberturaDoLoginTests(TestCase):
    """`_abrir_login` é a PRIMEIRA coisa que o worker faz depois de abrir o Chromium.

    Ele citava `GOTO_TENTATIVAS` sem importar o nome: toda tentativa de conectar a
    Amazon morria num NameError, o `except` do worker virava fase="erro" e o live
    view fechava sozinho — o sintoma relatado ("a tela abre e fecha"). Nenhum teste
    exercitava esta função, então o defeito era invisível para a suíte inteira.
    """

    class _PageQueNavega:
        def __init__(self):
            self.urls = []

        def goto(self, url, **_kwargs):
            self.urls.append(url)

    class _PageQueEstoura:
        def __init__(self):
            self.tentativas = 0

        def goto(self, _url, **_kwargs):
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

            self.tentativas += 1
            raise PlaywrightTimeoutError("timeout")

    def test_navegacao_bem_sucedida_abre_o_login_oficial(self):
        page = self._PageQueNavega()
        amazon_conexao._abrir_login(page)
        self.assertEqual(page.urls, [amazon_conexao.LOGIN_URL])

    def test_modo_compras_abre_a_conta_da_loja(self):
        page = self._PageQueNavega()
        amazon_conexao._abrir_login(page, shopper=True)
        self.assertEqual(page.urls, [amazon_conexao.SHOP_URL])

    def test_timeout_repetido_vira_erro_legivel_e_nao_nameerror(self):
        page = self._PageQueEstoura()
        with self.assertRaises(RuntimeError) as capturado:
            amazon_conexao._abrir_login(page)
        self.assertIn("Amazon demorou demais", str(capturado.exception))
        self.assertEqual(page.tentativas, amazon_conexao.GOTO_TENTATIVAS)


class TemplateLiveViewTests(TestCase):
    """Tela parada (CAPTCHA sendo lido) não pode acusar queda sozinha."""

    def setUp(self):
        # Caminho derivado deste arquivo, não do cwd: o CI roda `manage.py` a partir de
        # `python/` (.github/workflows/ci.yml), então o caminho relativo antigo
        # levantava FileNotFoundError e estes dois testes nunca chegavam a rodar.
        from django.conf import settings

        template = (
            Path(settings.BASE_DIR) / "apps" / "templates" / "scrapers" / "ml_conexao.html"
        )
        self.html = template.read_text(encoding="utf-8")

    def test_watchdog_conta_heartbeat_como_sinal_de_vida(self):
        # Quadro novo não é a única prova de vida: uma tela parada só produz
        # heartbeat, e medir a queda por `lastFrameAt` acusava interrupção com o
        # transporte saudável — era o loop de "Reconectando…" que travava o login.
        self.assertIn("lastAliveAt > 12000", self.html)
        self.assertNotIn("Date.now() - lastFrameAt > 12000", self.html)
        self.assertIn("lastAliveAt = Date.now();", self.html)

    def test_aviso_de_stream_nunca_engole_clique_nem_tecla(self):
        # O aviso cobria o canvas inteiro e interceptava ponteiro: quando a imagem
        # engasgava, o formulário do site sumia atrás dele e o login ficava
        # impossível de concluir.
        overlay = self.html.split(".stream-overlay {", 1)[1].split("}", 1)[0]
        self.assertIn("pointer-events:none", overlay)
        self.assertNotIn("inset:0", overlay)

    def test_canvas_so_e_redimensionado_quando_muda(self):
        # Reatribuir width/height limpa o canvas — era a origem do "piscando".
        self.assertIn(
            "if (canvas.width === viewport.width && canvas.height === viewport.height) return;",
            self.html,
        )

    def test_clique_simples_viaja_como_operacao_atomica(self):
        # Um CAPTCHA de imagens não pode depender de dois POSTs sequenciais para
        # completar down/up. O arraste continua explicitamente suportado.
        self.assertIn("push({t:'click'", self.html)
        self.assertIn("push({t:'down'", self.html)
        self.assertIn("gesture.dragging = true", self.html)

    def test_clique_leva_a_duracao_real_do_toque(self):
        # Um toque de 0ms não é um gesto que exista num usuário de verdade, e parte
        # dos widgets de desafio o ignora.
        self.assertIn("inicio:Date.now()", self.html)
        self.assertIn("holdMs:", self.html)

    def test_teclas_digitadas_durante_o_post_ainda_se_agrupam(self):
        # A guarda antiga era `!sending`, que proibia fundir justamente a rajada mais
        # rápida do usuário: cada tecla digitada durante o round-trip virava um evento
        # e uma chamada CDP separada no worker.
        self.assertIn("previous.seq > enviandoAteSeq", self.html)
        self.assertNotIn("!sending && event.t === 'char'", self.html)

    def test_duplo_clique_e_contado_no_cliente(self):
        # PointerEvent.detail é 0 por especificação em pointerdown/pointerup: derivar
        # clickCount dele dava sempre 1 e não havia como selecionar uma palavra.
        self.assertIn("function contarClique(", self.html)
        self.assertNotIn("clickCount:event.detail", self.html)
