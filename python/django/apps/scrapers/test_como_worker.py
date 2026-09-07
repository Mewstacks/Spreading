"""Rodar um teste com o mesmo contexto de banco que o worker tem.

`carga.operacao_pesada` — o lease do Chromium — falha FECHADO quando não há
`system_context`:

    if not in_system_context():
        yield False
        return

Isso é deliberado e está certo: uma request web não pode abrir Chromium por fora
da fila promovendo a própria conexão a worker. Os workers instalam o contexto
antes de chegar lá, e o comentário em `carga.py:50-58` diz exatamente isso.

Só que o mesmo arquivo abre com um atalho para os testes:

    if connection.vendor != "postgresql":
        yield True
        return

Sob SQLite o lease sempre concede, então nenhum teste precisava do contexto —
e o portão de verdade ficava sem cobertura. Contra PostgreSQL ele aparece: em
07/09/2026 foram 24 testes morrendo em `BrowserResourceUnavailable`, todos por
não estarem no contexto em que o código de produção roda.

`ComoWorker` fecha essa distância. Não é para deixar o teste verde: é para o teste
exercitar o mesmo caminho que a produção exercita, incluindo o lease de verdade.
"""
from django.test import SimpleTestCase

from apps.accounts.tenant import system_context


class ComoWorker:
    """Mixin: instala `system_context` pelo tempo do teste, como um worker faz.

    Usa `setUp` + `addCleanup`, o caminho documentado. Cada classe que aplica o
    mixin PRECISA chamar `super().setUp()` no seu próprio `setUp` — o teste
    `test_toda_classe_com_o_mixin_chama_super_setup` cobra isso, porque um mixin
    que silenciosamente não roda é pior do que não existir: parece coberto e não
    está.
    """

    def setUp(self):
        super().setUp()
        escopo = system_context()
        escopo.__enter__()
        self.addCleanup(escopo.__exit__, None, None, None)


class OMixinPrecisaMesmoRodarTests(SimpleTestCase):
    """Um mixin que não roda é pior do que não existir.

    A primeira versão deste mixin ancorava no `setUp` e a maioria das classes que
    o declaravam não chamava `super().setUp()` — então ele não tinha efeito
    nenhum, em silêncio, e os 24 testes que ele deveria consertar continuavam
    exatamente iguais. O arquivo parecia coberto e não estava.
    """

    def test_toda_classe_com_o_mixin_chama_super_setup(self):
        import inspect
        import re

        from apps.scrapers import (
            test_manual_scraping, test_ml_qr_onboarding, test_scraper_hardening,
            test_sources, tests,
        )

        faltando = []
        for modulo in (tests, test_scraper_hardening, test_sources,
                       test_ml_qr_onboarding, test_manual_scraping):
            fonte = inspect.getsource(modulo).split("\n")
            usa_mixin = False
            for indice, linha in enumerate(fonte):
                if linha.startswith("class "):
                    usa_mixin = "ComoWorker" in linha
                    classe = linha.split("(")[0].replace("class ", "")
                elif usa_mixin and re.match(r"    def setUp\(self\):\s*$", linha):
                    corpo = "\n".join(fonte[indice:indice + 12])
                    if "super().setUp()" not in corpo:
                        faltando.append(f"{modulo.__name__}.{classe}")
        self.assertEqual(faltando, [], (
            "Estas classes declaram ComoWorker mas não chamam super().setUp(), "
            "então o contexto de worker nunca é instalado."
        ))

    def test_o_mixin_instala_o_contexto(self):
        from apps.accounts.tenant import in_system_context

        class Sonda(ComoWorker, SimpleTestCase):
            def runTest(self):
                pass

        sonda = Sonda()
        sonda.setUp()
        try:
            self.assertTrue(in_system_context())
        finally:
            sonda.doCleanups()
        self.assertFalse(in_system_context())
