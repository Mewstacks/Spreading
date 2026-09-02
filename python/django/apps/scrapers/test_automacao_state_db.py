import os
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase, override_settings

from apps.scrapers import automacao_state as state
from apps.scrapers.models import AutomacaoEstado


@override_settings(APP_ENV="production")
class AutomacaoStateDatabaseTests(TestCase):
    def test_defaults_de_producao_ligam_jobs_no_primeiro_boot(self):
        with patch.dict(os.environ, {
            "AUTOMACAO_STATE_BACKEND": "database",
            "AUTOMACAO_DEFAULT_ENABLED": "scrape,envio",
        }):
            self.assertTrue(state.is_enabled("scrape"))
            self.assertTrue(state.is_enabled("envio"))
            self.assertFalse(state.is_enabled("relatorios"))

    def test_links_herda_scrape_ate_escolha_explicita(self):
        with patch.dict(os.environ, {
            "AUTOMACAO_STATE_BACKEND": "database",
            "AUTOMACAO_DEFAULT_ENABLED": "scrape",
        }):
            self.assertTrue(state.is_enabled("links"))
            state.set_enabled("links", False)
            self.assertFalse(state.is_enabled("links"))
            self.assertTrue(AutomacaoEstado.objects.get(job="links").configured)

    def test_heartbeat_e_mesclado_e_compartilhado(self):
        with patch.dict(os.environ, {"AUTOMACAO_STATE_BACKEND": "database"}):
            state.write_state("envio", fase="rodando")
            state.write_state("envio", total=3)
            atual = state.read_state("envio")
            self.assertEqual(atual["fase"], "rodando")
            self.assertEqual(atual["total"], 3)
            self.assertIn("atualizado_em", atual)

            state.clear_state("envio")
            self.assertEqual(state.read_state("envio"), {})


@override_settings(APP_ENV="production")
class AutomacaoStateFailSoftTests(TestCase):
    """Blip de Postgres não pode derrubar processo nem mentir 'desligado'.

    honcho mata o grupo inteiro quando um loop morre — 8 workers ou o
    gunicorn, dependendo da VM. Toda leitura/escrita de estado precisa
    absorver DatabaseError sem propagar E sem colapsar "não sei" em False
    (que desligaria a esteira em silêncio). Só escrita de intenção do
    usuário (set_enabled) continua levantando.
    """

    def setUp(self):
        state._ULTIMO_ENABLED.clear()
        state._ULTIMO_ESTADO.clear()
        state._ESTADO_NAO_PERSISTIDO.clear()
        state._ULTIMO_AVISO_DEGRADADO.clear()

    def test_is_enabled_cai_no_default_operacional_sem_historico(self):
        with patch.dict(os.environ, {
            "AUTOMACAO_STATE_BACKEND": "database",
            "AUTOMACAO_DEFAULT_ENABLED": "scrape,envio",
        }), patch.object(
            AutomacaoEstado.objects, "get_or_create",
            side_effect=DatabaseError("conexão fechada"),
        ):
            # "scrape" está no default operacional: nunca falso por erro.
            self.assertTrue(state.is_enabled("scrape"))
            # "cupons" não está no default e nunca foi lido com sucesso: aqui
            # o comportamento correto é refletir a ausência de histórico, não
            # inventar True — o teste documenta o piso, não um valor mágico.
            self.assertFalse(state.is_enabled("cupons"))

    def test_is_enabled_mantem_ultimo_valor_bom_durante_blip(self):
        with patch.dict(os.environ, {"AUTOMACAO_STATE_BACKEND": "database"}):
            state.set_enabled("cupons", True)
            self.assertTrue(state.is_enabled("cupons"))
            with patch.object(
                AutomacaoEstado.objects, "get_or_create",
                side_effect=DatabaseError("conexão fechada"),
            ):
                # Banco caiu DEPOIS de já sabermos que "cupons" está ligado.
                # Continuar True é a diferença entre "esteira pausada" e
                # "esteira desligada sem ninguém pedir".
                self.assertTrue(state.is_enabled("cupons"))

    def test_write_state_nunca_levanta_e_acumula_para_a_proxima_escrita_boa(self):
        with patch.dict(os.environ, {"AUTOMACAO_STATE_BACKEND": "database"}):
            with patch.object(
                AutomacaoEstado.objects, "select_for_update",
                side_effect=DatabaseError("conexão fechada"),
            ):
                resultado = state.write_state("scrape", fase="raspando", pagina=3)
                self.assertEqual(resultado["fase"], "raspando")
                self.assertTrue(resultado["estado_degradado"])
            # Banco voltou: a próxima escrita bem-sucedida empurra o que ficou
            # acumulado em memória durante o blip.
            estado = state.write_state("scrape", pagina=4)
            self.assertEqual(estado["fase"], "raspando")
            self.assertEqual(estado["pagina"], 4)

    def test_read_state_devolve_ultimo_conhecido_marcado_como_degradado(self):
        with patch.dict(os.environ, {"AUTOMACAO_STATE_BACKEND": "database"}):
            state.write_state("links", fase="gerando")
            with patch.object(
                AutomacaoEstado.objects, "get_or_create",
                side_effect=DatabaseError("conexão fechada"),
            ):
                lido = state.read_state("links")
                self.assertEqual(lido["fase"], "gerando")
                self.assertTrue(lido["estado_degradado"])
                # Nunca {} — worker_alive() leria isso como "processo morto",
                # uma segunda mentira em cima da primeira.
                self.assertNotEqual(lido, {})

    def test_clear_state_nao_levanta_em_blip(self):
        with patch.dict(os.environ, {"AUTOMACAO_STATE_BACKEND": "database"}):
            with patch.object(
                AutomacaoEstado.objects, "filter",
                side_effect=DatabaseError("conexão fechada"),
            ):
                state.clear_state("scrape")  # não deve levantar

    def test_links_herda_scrape_nao_levanta_em_blip(self):
        with patch.dict(os.environ, {"AUTOMACAO_STATE_BACKEND": "database"}):
            with patch.object(
                AutomacaoEstado.objects, "get_or_create",
                side_effect=DatabaseError("conexão fechada"),
            ):
                self.assertTrue(state.links_herda_scrape())

    def test_set_enabled_continua_levantando_em_blip(self):
        """Escrita de intenção do usuário não pode ser engolida em silêncio."""
        with patch.dict(os.environ, {"AUTOMACAO_STATE_BACKEND": "database"}):
            with patch.object(
                AutomacaoEstado.objects, "update_or_create",
                side_effect=DatabaseError("conexão fechada"),
            ):
                with self.assertRaises(DatabaseError):
                    state.set_enabled("scrape", False)
