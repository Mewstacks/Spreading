"""A lane de links cede Chromium para o preparo de cupons comprovados."""
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.scrapers.management.commands.automacao import _cupons_estao_processando


class PrioridadeDaLaneDeLinksTests(SimpleTestCase):
    @patch("apps.scrapers.management.commands.automacao.st.worker_alive")
    @patch("apps.scrapers.management.commands.automacao.st.read_state")
    def test_cupom_processando_tem_prioridade_sobre_link_generico(
        self, read_state, worker_alive,
    ):
        read_state.return_value = {"fase": "processando"}
        worker_alive.return_value = True

        self.assertTrue(_cupons_estao_processando())

    @patch("apps.scrapers.management.commands.automacao.st.worker_alive")
    @patch("apps.scrapers.management.commands.automacao.st.read_state")
    def test_cupom_ocioso_nao_bloqueia_links(
        self, read_state, worker_alive,
    ):
        read_state.return_value = {"fase": "aguardando"}
        worker_alive.return_value = True

        self.assertFalse(_cupons_estao_processando())
