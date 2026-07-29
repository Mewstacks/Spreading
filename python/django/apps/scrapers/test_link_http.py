"""Verificação de destino por HTTP puro (sem Chromium).

O caso mais importante aqui é o do challenge: o anti-bot do ML no IP de datacenter
da Fly redireciona navegações legítimas para login. Se isso virar "reprovado", uma
única janela de bloqueio derruba centenas de links bons e esvazia a tela de
Promoções. Tem de ser sempre TRANSITÓRIO.
"""
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.scrapers.scraper_mercadolivre import link_http


class RespostaFalsa:
    def __init__(self, url, texto="", status=200):
        self.url = url
        self.text = texto
        self.status_code = status


# HTML mínimo com os marcadores que o ML entrega no SSR.
def _pdp(nome="Smart TV 50 polegadas 4K", com_preco=True, riscado=False, extra=""):
    preco = ('<span class="andes-money-amount__fraction">1.799</span>'
             if com_preco else "")
    anterior = ('<s class="andes-money-amount--previous">'
                '<span class="andes-money-amount__fraction">2.499</span></s>'
                if riscado else "")
    return (
        '<html><body class="ui-pdp-container">'
        '<nav class="nav-header"></nav>'
        f'<h1 class="ui-pdp-title">{nome}</h1>'
        f'<div class="ui-pdp-price">{preco}{anterior}</div>'
        f'{extra}'
        # Enche o corpo para passar do limiar de "página curta demais" (challenge).
        f'<!-- {"x" * 25000} -->'
        '</body></html>'
    )


def _get(resposta):
    """Faz `_sessao().get(...)` devolver `resposta`."""
    return patch.object(link_http, "_sessao",
                        return_value=type("S", (), {"get": lambda *a, **k: resposta})())


URL_PDP = "https://www.mercadolivre.com.br/smart-tv/p/MLB123456"


class VerificacaoPorHttpTests(SimpleTestCase):

    def test_pdp_valida_com_nome_batendo_e_aprovada(self):
        with _get(RespostaFalsa(URL_PDP, _pdp())):
            r = link_http.relatorio_por_http(
                URL_PDP, nome_esperado="Smart TV 50 polegadas 4K", confiar_desconto=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r["is_pagina_produto"])
        self.assertIsNot(r["nome_confere"], False)

    def test_redirect_para_login_e_transitorio_nunca_reprovado(self):
        """O caso que protege o catálogo de um pico de anti-bot."""
        with _get(RespostaFalsa("https://www.mercadolivre.com.br/login?go=x", _pdp())):
            r = link_http.relatorio_por_http(URL_PDP, confiar_desconto=True)
        self.assertFalse(r["ok"])
        # É a substring que verificar_e_aprovar usa para decidir "transitorio".
        self.assertTrue(any("Falha ao abrir link" in e for e in r["erros"]))

    def test_status_de_bloqueio_e_transitorio(self):
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status):
                with _get(RespostaFalsa(URL_PDP, "", status=status)):
                    r = link_http.relatorio_por_http(URL_PDP, confiar_desconto=True)
                self.assertTrue(any("Falha ao abrir link" in e for e in r["erros"]))

    def test_corpo_curto_sem_marcadores_do_ml_e_transitorio(self):
        with _get(RespostaFalsa(URL_PDP, "<html><body>Verificando…</body></html>")):
            r = link_http.relatorio_por_http(URL_PDP, confiar_desconto=True)
        self.assertTrue(any("challenge" in e for e in r["erros"]))

    def test_excecao_de_rede_e_transitoria(self):
        class Explode:
            def get(self, *a, **k):
                raise OSError("conexão recusada")
        with patch.object(link_http, "_sessao", return_value=Explode()):
            r = link_http.relatorio_por_http(URL_PDP, confiar_desconto=True)
        self.assertTrue(any("Falha ao abrir link" in e for e in r["erros"]))

    def test_anuncio_pausado_e_reprovacao_legitima(self):
        corpo = _pdp(extra="<p>Anúncio pausado</p>")
        with _get(RespostaFalsa(URL_PDP, corpo)):
            r = link_http.relatorio_por_http(URL_PDP, confiar_desconto=True)
        self.assertFalse(r["ok"])
        # NÃO é transitório: o link deve ser reprovado de verdade.
        self.assertFalse(any("Falha ao abrir link" in e for e in r["erros"]))
        self.assertTrue(any("inativo" in e for e in r["erros"]))

    def test_404_e_reprovacao_legitima(self):
        with _get(RespostaFalsa(URL_PDP, _pdp(), status=404)):
            r = link_http.relatorio_por_http(URL_PDP, confiar_desconto=True)
        self.assertFalse(any("Falha ao abrir link" in e for e in r["erros"]))
        self.assertTrue(any("inativo" in e for e in r["erros"]))

    def test_nome_diferente_reprova(self):
        with _get(RespostaFalsa(URL_PDP, _pdp(nome="Geladeira Frost Free"))):
            r = link_http.relatorio_por_http(
                URL_PDP, nome_esperado="Smart TV 50 polegadas 4K UHD",
                confiar_desconto=True)
        self.assertFalse(r["ok"])
        self.assertFalse(r["nome_confere"])

    def test_confiar_desconto_pula_extracao_de_preco(self):
        """Para oferta/busca o preço não entra na decisão (aprovado_por_relatorio),
        e extrair custava 7s por link no caminho com browser."""
        with _get(RespostaFalsa(URL_PDP, _pdp(riscado=True))):
            r = link_http.relatorio_por_http(URL_PDP, confiar_desconto=True)
        self.assertTrue(r["ok"])
        self.assertIsNone(r["preco_visivel"])
        self.assertNotIn("preco_riscado", r)

    def test_cupom_exige_preco_e_desconto_na_pdp(self):
        with _get(RespostaFalsa(URL_PDP, _pdp(riscado=True))):
            r = link_http.relatorio_por_http(
                URL_PDP, nome_esperado="Smart TV 50 polegadas 4K",
                confiar_desconto=False)
        self.assertTrue(r["ok"])
        self.assertEqual(r["preco_visivel"], "R$ 1.799")
        self.assertTrue(r["preco_riscado"])

    def test_cupom_sem_desconto_visivel_reprova(self):
        with _get(RespostaFalsa(URL_PDP, _pdp(riscado=False))):
            r = link_http.relatorio_por_http(URL_PDP, confiar_desconto=False)
        self.assertFalse(r["ok"])

    def test_vitrine_social_reprova_cupom_com_motivo(self):
        social = "https://www.mercadolivre.com.br/social/loja-x?item=1"
        with _get(RespostaFalsa(social, _pdp())):
            r = link_http.relatorio_por_http(social, confiar_desconto=False)
        self.assertFalse(r["ok"])
        self.assertTrue(any("vitrine" in e for e in r["erros"]))

    def test_vitrine_social_serve_para_oferta(self):
        """Com confiar_desconto=True a vitrine é destino válido — mesma regra do
        caminho com browser (aprovado_por_relatorio)."""
        social = "https://www.mercadolivre.com.br/social/loja-x?item=1"
        with _get(RespostaFalsa(social, _pdp())):
            r = link_http.relatorio_por_http(social, confiar_desconto=True)
        self.assertTrue(r["ok"])

    def test_link_vazio(self):
        r = link_http.relatorio_por_http("", confiar_desconto=True)
        self.assertFalse(r["ok"])
        self.assertTrue(r["erros"])


class DespachoDeTransporteTests(SimpleTestCase):
    """O símbolo `verificar_link_afiliado` continua em link.py (3 testes fazem patch
    dele) e escolhe o transporte."""

    @override_settings(ML_VERIFICACAO_TRANSPORTE="http")
    def test_padrao_usa_http_sem_abrir_browser(self):
        from apps.scrapers.scraper_mercadolivre import link
        with patch.object(link, "iniciar_browser") as browser, \
             patch("apps.scrapers.scraper_mercadolivre.link_http.relatorio_por_http",
                   return_value={"ok": True}) as http:
            r = link.verificar_link_afiliado(URL_PDP, confiar_desconto=True)
        self.assertTrue(r["ok"])
        http.assert_called_once()
        browser.assert_not_called()

    @override_settings(ML_VERIFICACAO_TRANSPORTE="browser")
    def test_configuracao_volta_ao_browser(self):
        from apps.scrapers.scraper_mercadolivre import link
        with patch.object(link, "_verificar_com_browser",
                          return_value={"ok": False}) as navegador:
            link.verificar_link_afiliado(URL_PDP)
        navegador.assert_called_once()

    @override_settings(ML_VERIFICACAO_TRANSPORTE="http")
    def test_screenshot_forca_browser(self):
        """Só o Chromium tira print; pedir screenshot não pode cair no HTTP."""
        from apps.scrapers.scraper_mercadolivre import link
        with patch.object(link, "_verificar_com_browser",
                          return_value={"ok": False}) as navegador:
            link.verificar_link_afiliado(URL_PDP, screenshot_path="/tmp/x.png")
        navegador.assert_called_once()


class SemValidacaoDeSessaoRedundanteTests(SimpleTestCase):
    """A pré-checagem de sessão abria um Chromium inteiro (goto 45s + networkidle 8s),
    fechava, e só então abria o browser real. Não decidia nada — em timeout marcava
    "inconclusiva" e seguia. Quem detecta sessão caída é _abrir_link_builder.

    Ela foi REMOVIDA de `iniciar_browser`, não desligada: enquanto o parâmetro
    existisse, bastava um call-site novo esquecer `validar_sessao=False` para o
    Chromium extra voltar — e, no IP de datacenter da Fly, voltar junto o falso
    "sessão expirada" que ela produzia. Por isso o teste afere a ausência do
    parâmetro na assinatura, e não o valor passado.
    """

    def test_iniciar_browser_nao_tem_mais_pre_checagem(self):
        import inspect
        from apps.scrapers.auxiliar import iniciar_browser

        parametros = inspect.signature(iniciar_browser).parameters
        self.assertNotIn("validar_sessao", parametros)

    def _capturar_kwargs(self, alvo):
        from contextlib import contextmanager
        capturado = {}

        @contextmanager
        def _falso(*a, **kw):
            capturado.update(kw)
            raise RuntimeError("parar aqui: só queremos os kwargs")
            yield  # pragma: no cover

        return _falso, capturado

    def test_link_builder_de_item_unico_nao_valida_sessao(self):
        from apps.scrapers.scraper_mercadolivre import link
        falso, capturado = self._capturar_kwargs(link)
        with patch.object(link, "iniciar_browser", falso), \
             patch("apps.accounts.feature_flags.enabled_for_user", return_value=True):
            with self.assertRaises(RuntimeError):
                link.afiliate_link_builder("https://produto.mercadolivre.com.br/MLB-1")
        # Um único browser, e sem pedir validação: o kwarg nem existe mais.
        self.assertNotIn("validar_sessao", capturado)

    def test_lote_nao_valida_sessao(self):
        from apps.scrapers.scraper_mercadolivre import link

        class ProdutoFalso:
            id, link_afiliado, campanha_id = 1, "", ""
            link_produto = "https://produto.mercadolivre.com.br/MLB-123456"
            nome, origem = "Item", "oferta"

        falso, capturado = self._capturar_kwargs(link)
        with patch.object(link, "iniciar_browser", falso):
            with self.assertRaises(RuntimeError):
                link.gerar_links_em_lote([ProdutoFalso()])
        self.assertNotIn("validar_sessao", capturado)


class AbrirLinkBuilderTests(SimpleTestCase):
    """Distinguir anti-bot de sessão morta é a diferença entre "espere" e
    "reconecte sua conta" — e entre descartar ou não um lote de 50."""

    class PageFalsa:
        """Playwright mínimo: só o que _abrir_link_builder toca."""
        def __init__(self, urls, erros=None):
            self._urls = list(urls)
            self._erros = list(erros or [])
            self.url = ""
            self.gotos = 0

        def goto(self, *a, **kw):
            self.gotos += 1
            erro = self._erros.pop(0) if self._erros else None
            if erro:
                raise erro
            self.url = self._urls.pop(0) if self._urls else self.url

        def wait_for_load_state(self, *a, **kw):
            pass

        def get_by_test_id(self, *a, **kw):
            class _El:
                def is_visible(self_inner, *a, **kw):
                    return False
            return _El()

    def setUp(self):
        from apps.scrapers.scraper_mercadolivre import link
        self.link = link
        remendo = patch.object(link.time, "sleep")   # não dormir 3s no teste
        remendo.start()
        self.addCleanup(remendo.stop)

    def test_intersticial_vira_antibot_e_nao_login(self):
        """O caso do bug: challenge do anti-bot virava 'sessão expirada' e mandava
        o usuário reconectar uma conta que estava perfeita."""
        pagina = self.PageFalsa([
            "https://www.mercadolivre.com.br/gz/account-verification?go=x",
            "https://www.mercadolivre.com.br/gz/account-verification?go=x",
        ])
        with self.assertRaises(self.link.AntiBotError) as ctx:
            self.link._abrir_link_builder(pagina)
        self.assertNotIsInstance(ctx.exception, self.link.LoginError)
        self.assertEqual(pagina.gotos, 2)   # retentou antes de desistir

    def test_intersticial_que_libera_na_segunda_passa(self):
        pagina = self.PageFalsa([
            "https://www.mercadolivre.com.br/gz/account-verification?go=x",
            "https://www.mercadolivre.com.br/afiliados/linkbuilder#hub",
        ])
        self.link._abrir_link_builder(pagina)     # não levanta
        self.assertEqual(pagina.gotos, 2)

    def test_timeout_na_primeira_e_sucesso_na_segunda(self):
        pagina = self.PageFalsa(
            ["https://www.mercadolivre.com.br/afiliados/linkbuilder#hub"],
            erros=[TimeoutError("lento")],
        )
        self.link._abrir_link_builder(pagina)     # não levanta
        self.assertEqual(pagina.gotos, 2)

    def test_login_de_verdade_continua_levantando_login_error(self):
        """A detecção legítima não pode regredir."""
        pagina = self.PageFalsa([
            "https://www.mercadolivre.com/jms/mlb/lgz/msl/login/",
            "https://www.mercadolivre.com/jms/mlb/lgz/msl/login/",
        ])
        with self.assertRaises(self.link.LoginError):
            self.link._abrir_link_builder(pagina)

    def test_navegacao_que_nunca_responde_vira_auth_error(self):
        pagina = self.PageFalsa([], erros=[TimeoutError("x"), TimeoutError("y")])
        with self.assertRaises(self.link.AuthError) as ctx:
            self.link._abrir_link_builder(pagina)
        # A mensagem antiga mandava "Reconecte sua conta", que era o conselho errado.
        self.assertNotIn("Reconecte", str(ctx.exception))
