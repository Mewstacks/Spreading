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
from apps.accounts.tenant import system_context


class ComoWorker:
    """Mixin: instala `system_context` pelo tempo do teste, como um worker faz."""

    def setUp(self):
        self._escopo_de_worker = system_context()
        self._escopo_de_worker.__enter__()
        self.addCleanup(self._sair_do_escopo_de_worker)
        super().setUp()

    def _sair_do_escopo_de_worker(self):
        escopo, self._escopo_de_worker = getattr(
            self, "_escopo_de_worker", None), None
        if escopo is not None:
            escopo.__exit__(None, None, None)
