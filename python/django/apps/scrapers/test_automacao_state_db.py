import os
from unittest.mock import patch

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
