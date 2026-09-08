"""O portal do Mercado Livre estava sendo declarado morto enquanto funcionava.

Medido em produção em 08/09/2026, na conta da lules: a página do Link Builder
abria logada, com o textarea de URLs e o botão Gerar presentes e visíveis, e
`_linkbuilder_pronto` respondia False. O motivo é uma diferença de contrato que
não aparece lendo o código depressa — `Locator.is_enabled` **levanta**
`TimeoutError` quando o locator não assenta no prazo; não devolve False. Como a
chamada estava solta dentro do `try` que abraça a função inteira, o estouro
subia até o `except Exception: return False` do fim e o fallback logo abaixo —
preencher o campo e observar o botão habilitar, que é o que o próprio comentário
promete — nunca chegava a rodar.

O estrago passou do scraper: `ml_conexao` usa esta mesma função para julgar a
sessão. Então o sistema pedia reconexão de uma conta que esteve conectada o
tempo todo, e `links_erro`/`links_sem_sessao` se acumulavam com a causa errada
carimbada.

Estourar o prazo aqui significa "ainda não sei". A resposta a não saber é
tentar o fallback, não desistir.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from apps.scrapers.scraper_mercadolivre import link as ml_link

try:  # pragma: no cover - o ambiente de CI tem playwright instalado
    from playwright.sync_api import TimeoutError as ErroDePrazo
except Exception:  # pragma: no cover
    class ErroDePrazo(Exception):
        pass


class _Botao:
    """Botão que responde como o Andes respondeu em produção.

    `estouros` é quantas das primeiras chamadas a `is_enabled` levantam antes de
    o elemento assentar — é assim que o botão real se comportou enquanto o React
    o remontava.
    """

    def __init__(self, *, habilitado_com_texto=True, estouros=0):
        self.habilitado_com_texto = habilitado_com_texto
        self.estouros = estouros
        self.campo = None
        self.chamadas = 0

    def is_enabled(self, timeout=None):
        self.chamadas += 1
        if self.estouros > 0:
            self.estouros -= 1
            raise ErroDePrazo(f"Locator.is_enabled: Timeout {timeout}ms exceeded.")
        if not self.habilitado_com_texto:
            return False
        return bool(self.campo.valor)


class _Campo:
    def __init__(self, valor="", estoura_leitura=False):
        self.valor = valor
        self.estoura_leitura = estoura_leitura
        self.preenchimentos = []

    def input_value(self, timeout=None):
        if self.estoura_leitura:
            raise ErroDePrazo(f"Locator.input_value: Timeout {timeout}ms exceeded.")
        return self.valor

    def fill(self, valor, timeout=None):
        self.valor = valor
        self.preenchimentos.append(valor)


class _Pagina:
    url = ml_link._LB_URL

    def wait_for_timeout(self, ms):
        return None


def _cenario(campo, botao):
    botao.campo = campo
    return (
        patch.object(ml_link, "_campo_url_linkbuilder", return_value=campo),
        patch.object(ml_link, "_botao_gerar_linkbuilder", return_value=botao),
    )


class LinkbuilderProntoTests(SimpleTestCase):
    def _rodar(self, campo, botao):
        p1, p2 = _cenario(campo, botao)
        with p1, p2:
            return ml_link._linkbuilder_pronto(_Pagina())

    def test_prazo_estourado_na_sondagem_fria_nao_condena_o_portal(self):
        """A regressão de 08/09/2026, na forma exata em que apareceu.

        Primeira chamada estoura porque o botão ainda está sendo remontado; o
        formulário é perfeitamente funcional logo depois. Antes da correção isto
        devolvia False e o sistema mandava o usuário reconectar uma conta viva.
        """
        campo = _Campo(valor="")
        botao = _Botao(estouros=1)
        self.assertTrue(self._rodar(campo, botao))

    def test_a_leitura_do_valor_anterior_tambem_pode_estourar(self):
        """`input_value` estourou junto, em produção, e não pode condenar nada."""
        campo = _Campo(valor="", estoura_leitura=True)
        botao = _Botao(estouros=1)
        self.assertTrue(self._rodar(campo, botao))

    def test_botao_ja_habilitado_dispensa_mexer_no_formulario(self):
        campo = _Campo(valor="https://exemplo")
        botao = _Botao()
        self.assertTrue(self._rodar(campo, botao))
        self.assertEqual(
            campo.preenchimentos, [],
            "com o botão já habilitado não há por que tocar no formulário",
        )

    def test_botao_que_nunca_habilita_reprova(self):
        """Reprovar continua valendo — a correção não pode virar 'sempre pronto'."""
        campo = _Campo(valor="")
        botao = _Botao(habilitado_com_texto=False)
        self.assertFalse(self._rodar(campo, botao))

    def test_estouro_persistente_reprova(self):
        """Se nem depois de exercitar o formulário dá para saber, reprova."""
        campo = _Campo(valor="")
        botao = _Botao(estouros=99)
        self.assertFalse(self._rodar(campo, botao))

    def test_controle_ausente_reprova(self):
        for campo, botao in ((None, _Botao()), (_Campo(), None)):
            with self.subTest(campo=campo, botao=botao):
                with patch.object(ml_link, "_campo_url_linkbuilder", return_value=campo), \
                        patch.object(ml_link, "_botao_gerar_linkbuilder", return_value=botao):
                    self.assertFalse(ml_link._linkbuilder_pronto(_Pagina()))

    def test_o_formulario_fica_como_estava(self):
        """A sondagem não pode deixar rastro no campo do usuário."""
        campo = _Campo(valor="https://algo-que-o-usuario-digitou")
        botao = _Botao(estouros=1)
        self.assertTrue(self._rodar(campo, botao))
        self.assertEqual(campo.valor, "https://algo-que-o-usuario-digitou")

    def test_a_sondagem_nunca_submete(self):
        """Preencher prova que o controle funciona; clicar geraria atribuição."""
        campo = _Campo(valor="")
        botao = _Botao(estouros=1)
        self.assertTrue(self._rodar(campo, botao))
        self.assertFalse(
            hasattr(botao, "clicado"),
            "o botão Gerar não pode ser clicado por uma sondagem",
        )
