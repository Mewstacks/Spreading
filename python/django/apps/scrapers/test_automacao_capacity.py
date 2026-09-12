from django.test import SimpleTestCase

from apps.scrapers.management.commands.automacao import atraso_por_capacidade


class BackoffDeCapacidadeTests(SimpleTestCase):
    def test_aumenta_sem_monopolizar_o_chromium(self):
        self.assertEqual(
            [atraso_por_capacidade(numero) for numero in range(1, 7)],
            [15, 30, 60, 120, 120, 120],
        )

    def test_respeita_poll_e_teto_customizados(self):
        self.assertEqual(atraso_por_capacidade(1, poll=5, maximo=20), 5)
        self.assertEqual(atraso_por_capacidade(3, poll=5, maximo=20), 20)
