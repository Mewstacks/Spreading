from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone


class WorkerHealthBootTests(SimpleTestCase):
    def test_lane_with_delayed_boot_is_healthy_while_its_window_is_open(self):
        from apps.scrapers.management.commands import worker_health

        now = timezone.now()
        with patch.object(worker_health, "INICIADO_EM", now), patch.object(
            worker_health.st, "worker_alive", return_value=False,
        ):
            ok, body = worker_health._diagnostico(now + timedelta(seconds=30))

        self.assertTrue(ok)
        self.assertTrue(body["esteiras"]["scrape"]["iniciando"])

    def test_lane_is_unhealthy_after_its_expected_boot_window(self):
        from apps.scrapers.management.commands import worker_health

        now = timezone.now()
        with patch.object(worker_health, "INICIADO_EM", now), patch.object(
            worker_health.st, "worker_alive", return_value=False,
        ):
            ok, body = worker_health._diagnostico(now + timedelta(seconds=600))

        self.assertFalse(ok)
        self.assertFalse(body["esteiras"]["scrape"]["iniciando"])

    def test_heartbeat_da_maquina_anterior_nao_mente_no_restart(self):
        from apps.scrapers.management.commands import worker_health

        now = timezone.now()
        with patch.object(worker_health, "INICIADO_EM", now), patch.object(
            worker_health.st, "worker_alive", return_value=True,
        ), patch.object(
            worker_health.st, "read_state",
            return_value={"atualizado_em": (now - timedelta(seconds=5)).timestamp()},
        ):
            ok, body = worker_health._diagnostico(now + timedelta(seconds=30))

        self.assertTrue(ok)
        self.assertFalse(body["esteiras"]["scrape"]["viva"])
        self.assertTrue(body["esteiras"]["scrape"]["iniciando"])
