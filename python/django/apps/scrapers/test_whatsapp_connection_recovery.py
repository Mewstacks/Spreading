from unittest.mock import patch

from django.test import SimpleTestCase

from apps.scrapers.conexoes import estado_whatsapp
from apps.scrapers.send_pipeline import classify_result
from apps.scrapers.whatsapp_client import _safe_send_body, _request_json, _persistir_telemetria


class WhatsAppRecoveryStateTests(SimpleTestCase):
    def test_poll_preserves_actual_event_time_and_tolerates_invalid_capacity(self):
        with patch("apps.accounts.tenant.executar_orm_ou_direto", side_effect=lambda fn: fn()), patch(
            "apps.accounts.models.WhatsAppConnection.objects.filter",
        ) as query:
            _persistir_telemetria("isolated-session", {
                "conectado": True, "ultimo_evento_em": "2026-09-10T12:00:00Z",
                "capacidade": {"usadas": "invalid", "maximas": float("inf")},
            })
        fields = query.return_value.update.call_args.kwargs
        self.assertEqual(fields["last_event_at"].isoformat(), "2026-09-10T12:00:00+00:00")
        self.assertEqual(fields["capacity_used"], 0)
        self.assertEqual(fields["capacity_max"], 0)
    def test_transient_group_lookup_error_does_not_permanently_fail_publication(self):
        self.assertEqual(classify_result({
            "classe": "transitorio", "erro": "Não foi possível verificar o grupo de destino",
        }), "transient")
        self.assertEqual(classify_result({
            "classe": "transitorio", "resultado": "incerto", "repetir": False,
        }), "uncertain")

    def test_transport_telemetry_survives_allowlist_without_leaking_unknown_fields(self):
        result = _safe_send_body({"ack_ms": 240, "transporte_ms": 1600,
                                  "enviado_em": "2026-09-18T12:30:00Z", "token": "secret"})
        self.assertEqual(result["ack_ms"], 240)
        self.assertEqual(result["transporte_ms"], 1600)
        self.assertIn("2026-09-18T12:30:00", result["enviado_em"])
        self.assertNotIn("token", result)
        self.assertEqual(_safe_send_body({"ack_ms": float("inf"), "enviado_em": "invalid"}), {})

    def test_invalid_json_shape_is_a_controlled_service_error(self):
        with patch("apps.scrapers.whatsapp_client.requests.request") as request:
            request.return_value.json.return_value = []
            result = _request_json("GET", "/api/status", attempts=1)
        self.assertIn("erro", result)
        self.assertEqual(result["causa"], "ValueError")

    def test_all_runtime_recovery_phases_are_transient_not_qr_requests(self):
        for phase in ("iniciando", "preparando", "carregando", "autenticado",
                      "sincronizando", "reconectando", "recuperando", "reiniciando_qr"):
            with self.subTest(phase=phase), patch(
                "apps.scrapers.whatsapp_client.status",
                return_value={"conectado": False, "fase": phase},
            ):
                state = estado_whatsapp(session="isolated-session")
                self.assertFalse(state.conectado)
                self.assertEqual(state.detalhe, "conectando")
                self.assertEqual(state.availability_code, "recovering")

    def test_qr_still_requires_pairing(self):
        with patch("apps.scrapers.whatsapp_client.status",
                   return_value={"conectado": False, "fase": "qr"}):
            self.assertEqual(estado_whatsapp(session="isolated-session").detalhe,
                             "sem_pareamento")
