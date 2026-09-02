from contextlib import nullcontext
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase


COMMAND = "apps.accounts.management.commands.wa_capability_probe"


class WhatsAppCapabilityProbeTests(SimpleTestCase):
    def _queryset(self, connection):
        queryset = Mock()
        queryset.order_by.return_value.first.return_value = connection
        return queryset

    @patch(f"{COMMAND}.system_context", return_value=nullcontext())
    @patch(f"{COMMAND}.issue_capability", return_value="capability")
    @patch(f"{COMMAND}.requests.get")
    @patch(f"{COMMAND}.WhatsAppConnection.objects.filter")
    def test_selects_lules_and_requires_the_selected_session_ready(
        self, objects_filter, requests_get, _issue, _context,
    ):
        objects_filter.return_value = self._queryset(
            SimpleNamespace(instance_id="4")
        )
        denied = Mock(status_code=403)
        accepted = Mock(status_code=200)
        accepted.json.return_value = {
            "instancia": "4",
            "conectado": True,
            "fase": "conectado",
        }
        requests_get.side_effect = [denied, accepted]
        output = StringIO()

        call_command(
            "wa_capability_probe",
            username="lules",
            require_ready=True,
            stdout=output,
        )

        objects_filter.assert_called_once_with(
            organization__status="active",
            organization__personal_owner__username="lules",
        )
        self.assertIn("'lules'", output.getvalue())
        self.assertIn("conectado=True", output.getvalue())

    @patch(f"{COMMAND}.system_context", return_value=nullcontext())
    @patch(f"{COMMAND}.issue_capability", return_value="capability")
    @patch(f"{COMMAND}.requests.get")
    @patch(f"{COMMAND}.WhatsAppConnection.objects.filter")
    def test_require_ready_rejects_qr_phase_for_the_selected_account(
        self, objects_filter, requests_get, _issue, _context,
    ):
        objects_filter.return_value = self._queryset(
            SimpleNamespace(instance_id="4")
        )
        denied = Mock(status_code=401)
        accepted = Mock(status_code=200)
        accepted.json.return_value = {
            "instancia": "4",
            "conectado": False,
            "fase": "qr",
        }
        requests_get.side_effect = [denied, accepted]

        with self.assertRaisesMessage(CommandError, "fase='qr'"):
            call_command(
                "wa_capability_probe",
                username="lules",
                require_ready=True,
            )

    @patch(f"{COMMAND}.system_context", return_value=nullcontext())
    @patch(f"{COMMAND}.WhatsAppConnection.objects.filter")
    def test_unknown_username_fails_before_issuing_a_capability(
        self, objects_filter, _context,
    ):
        objects_filter.return_value = self._queryset(None)

        with self.assertRaisesMessage(CommandError, "'lules'"):
            call_command("wa_capability_probe", username="lules")

