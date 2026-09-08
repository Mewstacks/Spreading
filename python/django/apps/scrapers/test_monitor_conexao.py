"""Integração nunca ligada não é integração caída.

`{campo}_estado` é um booleano de três valores — None (nunca esteve de pé), True
e False — e None é a única marca que separa "nunca foi conectado" de "estava
conectado e caiu". O ramo que existe para proteger o primeiro caso gravava False
nesse campo, apagando a própria evidência: no tique seguinte `anterior` já era
False, o teste de "nunca conectou" dava falso, e a mesma integração
nunca-conectada caía no ramo de queda e virava `conexao_caiu` de nível error. A
proteção valia por uma única passagem.

Medido em produção em 08/09/2026, janela de sete dias: 99 `conexao_caiu` de
nível error, entre eles "Mercado Livre de teste1 está fora do ar: Nenhuma sessão
do Mercado Livre — conecte sua conta", de contas de teste que ninguém jamais
ligou — e zero `conexao_ausente` no mesmo relatório. Ruído conhecido em nível de
erro é o que esconde o erro desconhecido: na mesma manhã, um `conexao_caiu`
legítimo passou sem que ninguém visse.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from apps.scrapers import monitor_conexao


class _Perfil:
    """O mínimo que `_processar` toca: dois campos e um `save` que registra."""

    def __init__(self, estado=None, alerta_em=None, username="teste1"):
        self.ml_estado = estado
        self.alerta_ml_em = alerta_em
        self.user = SimpleNamespace(get_username=lambda: username)
        self.salvos = []

    def save(self, update_fields=None):
        self.salvos.append(tuple(update_fields or ()))


def _estado(detalhe="sem_sessao", conectado=False, motivo="conecte sua conta"):
    return SimpleNamespace(
        detalhe=detalhe, conectado=conectado, motivo=motivo,
        availability_code="sem_sessao", fonte="probe",
    )


class NuncaConectadoNaoViraQuedaTests(SimpleTestCase):
    COOLDOWN = timedelta(hours=6)

    def _processar(self, perfil, estado, agora=None):
        agora = agora or timezone.now()
        with patch("apps.scrapers.eventos.log_event") as log:
            enviou = monitor_conexao._processar(
                perfil, "Mercado Livre", "ml", estado, agora,
                self.COOLDOWN, lambda *a, **k: False,
            )
        return enviou, log

    def test_o_primeiro_encontro_avisa_em_nivel_de_aviso(self):
        perfil = _Perfil(estado=None)
        _enviou, log = self._processar(perfil, _estado())

        self.assertTrue(log.called)
        args, kwargs = log.call_args
        self.assertEqual(args[1], "conexao_ausente")
        self.assertEqual(kwargs.get("level"), "warning")

    def test_o_estado_continua_none_para_o_proximo_tique(self):
        """A regressão, na forma exata em que apareceu.

        Gravar False aqui era o que fazia a segunda passagem tratar uma conta
        nunca conectada como uma conta que caiu.
        """
        perfil = _Perfil(estado=None)
        self._processar(perfil, _estado())

        self.assertIsNone(
            perfil.ml_estado,
            "None é a verdade: esta conta nunca esteve de pé. Gravar False "
            "apaga a única marca que distingue isso de uma queda",
        )
        for campos in perfil.salvos:
            self.assertNotIn(
                "ml_estado", campos,
                "o estado não pode ser persistido neste ramo",
            )

    def test_o_segundo_tique_tambem_e_aviso_e_nao_erro(self):
        """Duas passagens seguidas, que é o que a produção faz a cada 5 minutos."""
        perfil = _Perfil(estado=None)
        agora = timezone.now()
        self._processar(perfil, _estado(), agora)
        _enviou, log = self._processar(
            perfil, _estado(), agora + self.COOLDOWN + timedelta(minutes=1),
        )

        args, kwargs = log.call_args
        self.assertEqual(
            args[1], "conexao_ausente",
            "a segunda passagem virava conexao_caiu de nível error",
        )
        self.assertEqual(kwargs.get("level"), "warning")

    def test_dentro_do_cooldown_nao_repete(self):
        """Manter None não pode virar um aviso a cada tique — 288 por dia."""
        perfil = _Perfil(estado=None)
        agora = timezone.now()
        self._processar(perfil, _estado(), agora)
        _enviou, log = self._processar(
            perfil, _estado(), agora + timedelta(minutes=5),
        )

        self.assertFalse(
            log.called,
            "o carimbo de alerta é o que segura a repetição de 5 em 5 minutos",
        )

    def test_queda_de_verdade_continua_sendo_erro(self):
        """Rebaixar não pode virar esconder: quem estava de pé e caiu é erro."""
        perfil = _Perfil(estado=True)
        _enviou, log = self._processar(
            perfil, _estado(detalhe="sessao_expirada", motivo="sessão expirada"),
        )

        args, kwargs = log.call_args
        self.assertEqual(args[1], "conexao_caiu")
        self.assertEqual(kwargs.get("level"), "error")

    def test_conta_que_conectou_depois_de_ausente_marca_de_pe(self):
        """None -> conectado grava True, e a partir daí uma queda é queda."""
        perfil = _Perfil(estado=None)
        self._processar(perfil, _estado(detalhe="ok", conectado=True))

        self.assertTrue(perfil.ml_estado)
