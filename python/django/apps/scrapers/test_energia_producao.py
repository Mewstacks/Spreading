"""O agendamento de liga/desliga da produção tem de concordar consigo mesmo.

Em 08/09/2026 a produção passou a manhã inteira fora do ar — incluindo a janela
de envio das 08:00 — e nada no repositório teria avisado. A causa foi o
agendador do GitHub descartar os dois religamentos, mas o que tornou o buraco
possível foi um conjunto de acordos tácitos entre dois arquivos que ninguém
verifica: o workflow declara horários, o passo `Resolve action` traduz cada
horário em `start`/`stop`, e o script de energia decide, pelo relógio, se aceita
a parada.

Três arquivos, nenhum ponto de checagem. Um cron acrescentado sem entrada no
mapa faz o job sair com `exit 1` e o religamento não acontece; um cron de parada
movido para fora da janela faz a produção nunca mais dormir, e a conta sobe sem
ninguém perceber. Ambos falham em silêncio, de madrugada, quando não há mais
nada de pé para reclamar.

Estes testes leem os arquivos de verdade e cobram a concordância.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from django.test import SimpleTestCase


def _raiz_do_repositorio() -> Path | None:
    """Sobe até achar o repositório. `None` quando só o `python/` foi copiado."""
    for diretorio in Path(__file__).resolve().parents:
        if (diretorio / ".github" / "workflows").is_dir():
            return diretorio
    return None


_RAIZ = _raiz_do_repositorio()
_WORKFLOW = _RAIZ / ".github" / "workflows" / "fly-nightly-power.yml" if _RAIZ else None
_SCRIPT = _RAIZ / "deploy" / "fly-nightly-power.sh" if _RAIZ else None

_TEM_ARQUIVOS = bool(
    _WORKFLOW and _WORKFLOW.is_file() and _SCRIPT and _SCRIPT.is_file()
)
_MOTIVO = "fora de um checkout completo do repositório"


def _ler(caminho: Path) -> str:
    return caminho.read_text(encoding="utf-8")


def _crons(workflow: str) -> list[str]:
    return re.findall(r'- cron: "([^"]+)"', workflow)


def _mapa(workflow: str) -> dict[str, str]:
    achados = re.findall(r'"([0-9][^"]*\*)"\)\s*ACAO=(\w+)', workflow)
    return dict(achados)


def _constante(script: str, nome: str) -> int:
    achado = re.search(rf"^readonly {nome}=(\d+)$", script, re.MULTILINE)
    assert achado, f"{nome} sumiu de fly-nightly-power.sh"
    return int(achado.group(1))


def _hora_brt(cron: str) -> int:
    """Cron do GitHub é UTC; o Brasil não tem mais horário de verão (UTC-3)."""
    _minuto, hora_utc = cron.split()[0], int(cron.split()[1])
    return (hora_utc - 3) % 24


@unittest.skipUnless(_TEM_ARQUIVOS, _MOTIVO)
class AgendamentoDeEnergiaTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.workflow = _ler(_WORKFLOW)
        self.script = _ler(_SCRIPT)

    def test_todo_cron_tem_acao_declarada(self):
        """Cron sem entrada no mapa faz o job sair com `exit 1`.

        E um `exit 1` no religamento é indistinguível, de fora, de não ter
        religamento nenhum.
        """
        crons = _crons(self.workflow)
        mapa = _mapa(self.workflow)
        self.assertTrue(crons, "o workflow perdeu todos os agendamentos")
        for cron in crons:
            self.assertIn(
                cron, mapa,
                f"o cron {cron!r} não tem entrada em `Resolve action`; "
                "o job sairia com exit 1 e a produção não subiria",
            )

    def test_toda_acao_declarada_tem_cron(self):
        """Entrada órfã no mapa é agendamento que alguém acha que existe."""
        crons = set(_crons(self.workflow))
        for horario in _mapa(self.workflow):
            self.assertIn(
                horario, crons,
                f"{horario!r} está no mapa de ações mas não é um cron; "
                "ninguém vai disparar isso",
            )

    def test_nenhum_agendamento_no_topo_da_hora(self):
        """`:00` é o pico de carga, e é onde o GitHub mais descarta execução.

        A doc do GitHub Actions diz que o evento `schedule` pode atrasar em
        períodos de alta carga e nomeia o início de cada hora como um deles. Em
        08/09/2026 os dois religamentos foram descartados; quatro das seis
        tentativas daquele dia estavam em `:00`.
        """
        for cron in _crons(self.workflow):
            minuto = cron.split()[0]
            self.assertNotEqual(
                minuto, "0",
                f"o cron {cron!r} está no topo da hora, a fila mais cheia; "
                "usar minuto quebrado é de graça",
            )

    def test_existe_exatamente_uma_parada(self):
        acoes = list(_mapa(self.workflow).values())
        self.assertEqual(
            acoes.count("stop"), 1,
            "a produção precisa de uma parada — e de só uma",
        )
        self.assertGreaterEqual(
            acoes.count("start"), 2,
            "religamento sem repetição foi o que causou o apagão de 08/09/2026",
        )

    def test_a_parada_agendada_cabe_na_janela_que_o_script_aceita(self):
        """O script recusa parada fora da janela. Um cron fora dela nunca para.

        Os dois arquivos podem divergir sem que nada quebre visivelmente: o job
        roda, o script recusa educadamente, sai 0, e a produção simplesmente
        nunca mais dorme. A conta sobe e o CI fica verde.
        """
        mapa = _mapa(self.workflow)
        parada = next(c for c, acao in mapa.items() if acao == "stop")

        inicio = _constante(self.script, "SONO_INICIO_H")
        religamento = _constante(self.script, "RELIGAMENTO_H")
        minimo = _constante(self.script, "SONO_MINIMO_H")
        ultima_hora_aceita = religamento - minimo

        hora = _hora_brt(parada)
        self.assertTrue(
            inicio <= hora <= ultima_hora_aceita,
            f"a parada está agendada para {hora}h BRT, fora da janela que "
            f"fly-nightly-power.sh aceita ({inicio}h-{ultima_hora_aceita}h): "
            "o script recusaria e a produção nunca dormiria",
        )

    def test_a_janela_de_parada_deixa_sono_que_pague_o_risco(self):
        """Parar por pouco tempo é o pior dos dois mundos.

        Uma hora de sono poupa cerca de R$0,37. Uma parada aceita perto do
        religamento compra centavos e arrisca a manhã de envio inteira, porque
        se o religamento for descartado — e ele é, foi assim em 08/09/2026 —
        não sobra nada de pé para levantar worker e WhatsApp.
        """
        minimo = _constante(self.script, "SONO_MINIMO_H")
        self.assertGreaterEqual(
            minimo, 3,
            "sono mínimo abaixo de 3h não paga o risco de o religamento falhar",
        )

    def test_o_primeiro_religamento_precede_a_janela_de_envio(self):
        """A janela de envio abre às 08:00; o WhatsApp leva ~50s autenticando."""
        starts = [
            _hora_brt(cron)
            for cron, acao in _mapa(self.workflow).items()
            if acao == "start"
        ]
        self.assertLess(
            min(starts), 8,
            "o primeiro religamento tem de vir antes das 08:00 BRT, senão a "
            "primeira janela de envio do dia sai vazia",
        )
