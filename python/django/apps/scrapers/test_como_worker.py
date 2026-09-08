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
from django.test import SimpleTestCase, TestCase

from apps.accounts.tenant import system_context


class ComoWorker:
    """Mixin: instala `system_context` pelo tempo do teste, como um worker faz.

    Usa `setUp` + `addCleanup`, o caminho documentado. Cada classe que aplica o
    mixin PRECISA chamar `super().setUp()` no seu próprio `setUp` — o teste
    `test_toda_classe_com_o_mixin_chama_super_setup` cobra isso, porque um mixin
    que silenciosamente não roda é pior do que não existir: parece coberto e não
    está.
    """

    # Recursos que o teste declara já deter. `leased_resource` e
    # `machine_resource_slot` são REENTRANTES por desenho — um worker que já
    # segura o Chromium não pede de novo ao entrar numa função aninhada, e é esse
    # mesmo mecanismo que se usa aqui.
    #
    # Sem isto, um teste que exercita `gerar_links_em_lote` ou `mapear_ofertas`
    # contra PostgreSQL morre em `BrowserResourceUnavailable`: o lease é uma linha
    # em `ResourceLease` mais um flock de arquivo, nenhum dos dois concedido a um
    # processo de teste. Sob SQLite o lease inteiro é curto-circuitado
    # (`carga.operacao_pesada` devolve True), então nada disso aparecia.
    RECURSOS_DO_WORKER = ("django_chromium",)
    # Leases cuja chave só existe em tempo de execução: a sessão de site do ML é
    # `ml_site_session:<uuid da organização>`, e a organização nasce no `setUp` da
    # própria subclasse, depois deste mixin. Um prefixo cobre isso sem que cada
    # teste tenha de adivinhar o UUID.
    PREFIXOS_DO_WORKER = ("ml_site_session:",)

    def setUp(self):
        super().setUp()
        escopo = system_context()
        escopo.__enter__()
        self.addCleanup(escopo.__exit__, None, None, None)
        self._conceder_recursos()

    def _conceder_recursos(self):
        if not self.RECURSOS_DO_WORKER and not self.PREFIXOS_DO_WORKER:
            return
        from apps.scrapers import resource_control

        prefixos = tuple(self.PREFIXOS_DO_WORKER)

        class _Detidos(dict):
            """Dict que também responde por prefixo, para as chaves dinâmicas."""

            def get(self, chave, padrao=None):
                if chave in self:
                    return dict.get(self, chave)
                if prefixos and str(chave).startswith(prefixos):
                    return f"teste-{chave}"
                return padrao

        detidos = _Detidos(resource_control._held_resources.get() or {})
        maquina = set(resource_control._held_machine_resources.get() or set())
        for recurso in self.RECURSOS_DO_WORKER:
            detidos.setdefault(recurso, f"teste-{recurso}")
            maquina.add(recurso)
        token = resource_control._held_resources.set(detidos)
        token_maquina = resource_control._held_machine_resources.set(maquina)
        self.addCleanup(resource_control._held_machine_resources.reset, token_maquina)
        self.addCleanup(resource_control._held_resources.reset, token)


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


class OMixinInstalaOContextoTests(ComoWorker, TestCase):
    """A prova direta: uma classe que usa o mixin está no contexto de worker.

    A versão anterior instanciava uma sonda e chamava `setUp()` na mão. Isso pula
    `_pre_setup`, então não há conexão de teste montada — e sob PostgreSQL o
    `system_context` consulta `pg_roles` para conferir a role. Usar o mixin de
    verdade, na própria classe, mede a mesma coisa sem simular o runner.
    """

    def test_o_teste_roda_dentro_do_contexto_de_sistema(self):
        from apps.accounts.tenant import in_system_context

        self.assertTrue(in_system_context())

    def test_o_recurso_do_worker_esta_concedido(self):
        from apps.scrapers import resource_control

        detidos = resource_control._held_resources.get() or {}
        self.assertIn("django_chromium", detidos)
