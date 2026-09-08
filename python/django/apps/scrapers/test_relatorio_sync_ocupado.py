"""Recurso ocupado não é falha, e falha nossa não pode virar frase genérica.

Duas coisas diferentes chegavam ao mesmo lugar em `sincronizar`:

1. **Perder a disputa pelo Chromium ou pela sessão de relatório.** É fila
   funcionando. Entrava como `sync_failed` de nível error e empurrava a próxima
   tentativa em SEIS HORAS — alarme por um lock que se solta em segundos, e o
   relatório parado o resto do dia por causa de uma espera.

2. **Toda falha operacional nossa.** O tratador redigia a mensagem antes de
   gravar, o que é certo para exceções ARBITRÁRIAS (um erro qualquer pode trazer
   URL com token dentro) e errado para `ReportSyncError`, cuja mensagem é escrita
   por nós e só contém marketplace, formato e nomes de coluna. O resultado é que
   "exportação vazia" e "cabeçalhos não reconhecidos" chegavam idênticos —
   "falha operacional durante a sincronização (ReportSyncError)" — e as 34
   ocorrências medidas em produção em 08/09/2026 não diziam nada a ninguém.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.scrapers import relatorios
from apps.scrapers.models import RelatorioSync


class _Adapter:
    """Adapter que explode do jeito pedido, antes de tocar em qualquer portal."""

    def __init__(self, erro):
        self.erro = erro

    def fetch(self, *args, **kwargs):
        raise self.erro


class SyncOcupadoNaoEFalhaTests(TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(
            username="dono", password="x" * 12,
        )

    def _sincronizar(self, erro):
        # `log_event` esta ligado ao modulo `relatorios` desde o import, entao o
        # patch tem de ser nesse nome. E interessa a ULTIMA chamada: a primeira e
        # sempre `sync_started`.
        with patch.dict(relatorios.ADAPTERS, {"mercadolivre": _Adapter(erro)}), \
                patch("apps.scrapers.relatorios.log_event") as log:
            sync = relatorios.sync_marketplace(self.user, "mercadolivre")
        return sync, log

    def test_ocupado_nao_e_erro(self):
        sync, log = self._sincronizar(
            relatorios.ReportSyncBusy("Chromium ocupado por outra tarefa; tente novamente.")
        )

        self.assertEqual(sync.status, "ocupado")
        self.assertEqual(sync.prerequisite_code, "resource_busy")
        args, kwargs = log.call_args
        self.assertEqual(args[1], "sync_ocupado")
        self.assertEqual(
            kwargs.get("level"), "warning",
            "disputa por navegador é fila funcionando, não incidente",
        )

    def test_ocupado_tenta_de_novo_em_minutos_nao_em_horas(self):
        """Seis horas de recuo por um lock de segundos parava o relatório o dia."""
        antes = timezone.now()
        sync, _log = self._sincronizar(
            relatorios.ReportSyncBusy("Sessão de relatório ocupada por outra tarefa.")
        )

        espera = sync.proxima_execucao - antes
        self.assertLess(
            espera.total_seconds(), 60 * 60,
            "o recuo de ocupado tem de ser de minutos; seis horas era o do erro",
        )

    def test_falha_nossa_chega_legivel(self):
        """A mensagem de `ReportSyncError` é nossa e diz qual foi o problema."""
        sync, log = self._sincronizar(
            relatorios.ReportSyncError("mercadolivre: exportação vazia.")
        )

        self.assertEqual(sync.status, "erro")
        args, kwargs = log.call_args
        self.assertEqual(args[1], "sync_failed")
        self.assertEqual(kwargs.get("level"), "error")
        self.assertIn(
            "exportação vazia", args[2],
            "sem a mensagem real, toda falha de sync vira a mesma frase e "
            "ninguém consegue diagnosticar nenhuma",
        )
        self.assertIn("exportação vazia", sync.erro)

    def test_excecao_arbitraria_continua_redigida(self):
        """A redação existe por um motivo, e ele não mudou.

        Um erro que não é nosso pode trazer qualquer coisa na mensagem — URL com
        token, dado do usuário. Esse caminho continua gravando só a categoria.
        """
        sync, log = self._sincronizar(
            RuntimeError("https://portal/callback?token=SEGREDO-NAO-PODE-VAZAR")
        )

        args, _kwargs = log.call_args
        self.assertEqual(args[1], "sync_failed")
        self.assertNotIn("SEGREDO-NAO-PODE-VAZAR", args[2])
        self.assertNotIn("SEGREDO-NAO-PODE-VAZAR", sync.erro)
        self.assertIn("RuntimeError", args[2])

    def test_ocupado_e_um_estado_declarado_do_modelo(self):
        """Status fora de `choices` passa em runtime e reprova no `full_clean`."""
        declarados = {valor for valor, _rotulo in RelatorioSync.STATUS}
        self.assertIn("ocupado", declarados)

    def test_a_tela_de_saude_sabe_explicar_o_evento(self):
        """Evento sem verbete aparece cru para quem abre a tela."""
        from apps.scrapers.saude import CATALOGO

        self.assertIn("sync_ocupado", CATALOGO)
        self.assertTrue(CATALOGO["sync_ocupado"]["titulo"])

    def test_a_contencao_realmente_levanta_a_classe_de_ocupado(self):
        """O tratador certo não adianta se o `raise` continuar sendo o antigo.

        Os testes acima injetam a exceção pronta, então provam o TRATADOR e não
        os pontos de disparo. Sem esta verificação, trocar `ReportSyncBusy` de
        volta por `ReportSyncError` nos dois pontos de contenção passa verde e a
        regressão volta inteira — foi assim que o ramo de "nunca conectado"
        sobreviveu quebrado.
        """
        import inspect

        fonte = inspect.getsource(relatorios)
        for trecho in ("Chromium ocupado por outra tarefa",
                       "Sessão de relatório ocupada por outra tarefa"):
            linha = next(
                (l for l in fonte.splitlines() if trecho in l and "raise" in l),
                None,
            )
            self.assertIsNotNone(linha, f"o ponto de contenção {trecho!r} sumiu")
            self.assertIn(
                "ReportSyncBusy", linha,
                f"{trecho!r} precisa levantar ReportSyncBusy; como "
                "ReportSyncError comum ele volta a ser erro com recuo de 6h",
            )
