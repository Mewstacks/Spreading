import tempfile
from datetime import date, timedelta
from unittest.mock import patch
from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.db import OperationalError, connection
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.scrapers.models import LinkAfiliadoUsuario, Produto, RelatorioSync


class CatalogoUniversalTests(TestCase):
    def test_catalogo_universal_nao_entra_no_ranking_ou_na_fila(self):
        user = get_user_model().objects.create_user("catalogo", password="x")
        produto = Produto.objects.create(
            marketplace="mercadolivre", origem="oferta", nome="Catálogo", preco_sem_desconto=100,
            preco_com_cupom=50, link_produto="https://www.mercadolivre.com.br/up/MLBU123",
            estado="invalido", falha_verificacao="Catálogo universal sem anúncio individual afiliável.")
        LinkAfiliadoUsuario.objects.create(usuario=user, produto=produto, estado="nao_afiliavel")
        from apps.scrapers.ofertas import selecionar_item_para_grupo
        self.assertEqual(selecionar_item_para_grupo(usuario=user), [])
        self.assertTrue("MLBU" in produto.link_produto)


class ReportQueueTests(TestCase):
    def test_due_queue_prioritizes_oldest_sync_instead_of_first_users(self):
        users = [get_user_model().objects.create_user(f"report-{n}", password="x") for n in range(22)]
        oldest = users[-1]
        for idx, user in enumerate(users):
            RelatorioSync.objects.create(usuario=user, marketplace="mercadolivre",
                                         proxima_execucao=timezone.now() - timedelta(minutes=idx))
        with patch("apps.scrapers.relatorios.ADAPTERS", {"mercadolivre": object()}), \
             patch("apps.scrapers.relatorios.sync_marketplace", side_effect=lambda u, m: (u.id, m)):
            from apps.scrapers.relatorios import sync_due_reports
            processed = sync_due_reports(limit=1)
        self.assertEqual(processed[0][0], oldest.id)


class ReportSessionTests(TestCase):
    def test_amazon_session_is_isolated_and_decrypted_only_in_memory(self):
        from apps.scrapers.report_sessions import (
            delete_report_state, encrypted_state_path, has_report_session,
            load_report_state, save_report_state,
        )

        first = get_user_model().objects.create_user("session-first", password="x")
        second = get_user_model().objects.create_user("session-second", password="x")
        state = {"cookies": [{"name": "session", "value": "opaque"}], "origins": []}
        with tempfile.TemporaryDirectory() as directory, override_settings(
            ML_AUTH_DIR=directory,
            SECRETS_FERNET_KEY="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        ):
            save_report_state(first, "amazon", state)
            self.assertTrue(has_report_session(first, "amazon"))
            self.assertFalse(has_report_session(second, "amazon"))
            self.assertNotEqual(encrypted_state_path(first, "amazon"), encrypted_state_path(second, "amazon"))
            self.assertEqual(load_report_state(first, "amazon"), state)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT encrypted_state FROM accounts_browsersession "
                    "WHERE user_id = %s AND provider = %s",
                    [first.id, "amazon"],
                )
                persisted = cursor.fetchone()[0].encode()
            self.assertNotIn(b"opaque", persisted)
            save_report_state(first, "amazon_shop", state)
            self.assertTrue(has_report_session(first, "amazon_shop"))
            delete_report_state(first, "amazon_shop")
            self.assertFalse(has_report_session(first, "amazon_shop"))
            self.assertTrue(has_report_session(first, "amazon"))

    def test_legacy_file_is_migrated_once_to_shared_database(self):
        import base64
        import json
        from apps.accounts.crypto import encrypt
        from apps.accounts.models import BrowserSession
        from apps.scrapers.report_sessions import (
            encrypted_state_path, has_report_session, load_report_state,
        )

        user = get_user_model().objects.create_user("legacy-report-session")
        state = {"cookies": [{"name": "legacy", "value": "secret"}], "origins": []}
        with tempfile.TemporaryDirectory() as directory, override_settings(
            ML_AUTH_DIR=directory,
            SECRETS_FERNET_KEY="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        ):
            path = encrypted_state_path(user, "amazon", criar=True)
            encoded = base64.b64encode(json.dumps(state).encode()).decode()
            path.write_text(encrypt(encoded), encoding="utf-8")

            self.assertTrue(has_report_session(user, "amazon"))
            self.assertEqual(load_report_state(user, "amazon"), state)
            self.assertTrue(BrowserSession.objects.filter(
                user=user, provider="amazon",
            ).exists())
            self.assertFalse(path.exists())

    def test_three_real_suspicions_request_reconnect_and_success_recovers(self):
        from apps.scrapers.report_sessions import (
            has_report_session, registrar_veredito, save_report_state,
        )

        user = get_user_model().objects.create_user("report-probe-policy")
        with override_settings(
            SECRETS_FERNET_KEY="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        ):
            save_report_state(user, "amazon_shop", {"cookies": [], "origins": []})
            for attempt in range(1, 4):
                snapshot = registrar_veredito(
                    user, "amazon_shop", "suspeito", "session_expired",
                )
                self.assertEqual(snapshot["falhas"], attempt)
                self.assertEqual(has_report_session(user, "amazon_shop"), attempt < 3)
            registrar_veredito(user, "amazon_shop", "conectado", "checkout_opened")
            self.assertTrue(has_report_session(user, "amazon_shop"))

    def test_report_parser_marks_login_page_as_reconnect_required(self):
        from apps.scrapers.relatorios import ReportSyncActionRequired, _extract_table_rows

        class PasswordLocator:
            def count(self):
                return 1

        class LoginPage:
            def locator(self, _selector):
                return PasswordLocator()

        with self.assertRaises(ReportSyncActionRequired):
            _extract_table_rows(LoginPage(), "amazon", timezone.localdate(), timezone.localdate())

    def test_report_csv_is_mapped_by_header_not_column_position(self):
        from apps.scrapers.relatorios import _parse_delimited_report

        report_day = date(2026, 7, 17)
        rows = _parse_delimited_report(
            "Comissão;Etiqueta;Data;Cliques;Conversões;Receita;Pedidos\n"
            "R$ 12,50;grupo-casa;17/07/2026;9;1;R$ 199,90;2\n".encode(),
            "amazon", report_day, report_day,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].etiqueta, "grupo-casa")
        self.assertEqual(rows[0].cliques, 9)
        self.assertEqual(rows[0].pedidos, 2)
        self.assertEqual(rows[0].receita, 199.90)
        self.assertEqual(rows[0].comissao, 12.50)


class HeavyPipelineLockTests(SimpleTestCase):
    @patch("apps.scrapers.carga.leased_resource")
    @patch("apps.scrapers.carga.connections")
    @patch("apps.scrapers.carga.in_system_context", return_value=True)
    def test_postgres_lease_releases_only_when_acquired(
        self, _system_context, connections, lease,
    ):
        from apps.scrapers.carga import operacao_pesada

        connection = connections.__getitem__.return_value
        connection.vendor = "postgresql"
        lease.return_value.__enter__.return_value = (True, {"owner_kind": "scheduled"})

        with operacao_pesada() as acquired:
            self.assertTrue(acquired)

        lease.assert_called_once_with(
            "django_chromium", owner_kind="scheduled", organization=None,
        )
        lease.return_value.__exit__.assert_called_once()

    @patch("apps.scrapers.carga.leased_resource")
    @patch("apps.scrapers.carga.connections")
    @patch("apps.scrapers.carga.in_system_context", return_value=False)
    def test_request_web_nao_adquire_slot_global(
        self, _system_context, connections, lease,
    ):
        from apps.scrapers.carga import operacao_pesada

        connections.__getitem__.return_value.vendor = "postgresql"
        with operacao_pesada() as acquired:
            self.assertFalse(acquired)
        lease.assert_not_called()


class DatabaseUnavailableMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_database_error_becomes_retryable_503(self):
        from core.middleware import DatabaseUnavailableMiddleware

        def unavailable(_request):
            raise OperationalError("server closed the connection unexpectedly")

        response = DatabaseUnavailableMiddleware(unavailable)(self.factory.get("/scrapers/whatsapp/"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "15")
        self.assertNotIn("unexpectedly", response.content.decode())

    @patch("core.middleware.connections")
    def test_healthz_bypasses_session_stack_and_returns_503(self, connections):
        from core.middleware import DatabaseUnavailableMiddleware

        connection = connections.__getitem__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = OperationalError("database down")
        downstream = MagicMock()

        response = DatabaseUnavailableMiddleware(downstream)(self.factory.get("/healthz"))

        self.assertEqual(response.status_code, 503)
        downstream.assert_not_called()


class LockComEsperaTests(SimpleTestCase):
    """O botão manual disputa o MESMO lease do worker. Sem isso, clicar
    enquanto o worker está no Link Builder abria um segundo Chromium no mesmo
    portal SSO com a mesma sessão — e o ML derrubava um dos dois para login."""

    @staticmethod
    def _leases(respostas):
        from contextlib import contextmanager

        @contextmanager
        def lease(*args, **kwargs):
            yield respostas.pop(0), {}
        return lease

    def test_espera_e_consegue_na_terceira(self):
        from apps.scrapers import carga
        conexao = MagicMock(vendor="postgresql")
        avisos = []
        with patch.object(carga, "connections", {"default": conexao}), \
             patch.object(carga, "in_system_context", return_value=True), \
             patch.object(carga, "leased_resource", self._leases([False, False, True])), \
             patch.object(carga.time, "sleep"):
            with carga.operacao_pesada_com_espera(
                    poll_s=0, aviso=avisos.append) as conseguiu:
                self.assertTrue(conseguiu)
        self.assertEqual(len(avisos), 2)

    def test_timeout_nao_adquire_e_nao_desbloqueia(self):
        """Soltar um lock que não é nosso derrubaria o worker no meio do lote."""
        from apps.scrapers import carga
        conexao = MagicMock(vendor="postgresql")
        with patch.object(carga, "connections", {"default": conexao}), \
             patch.object(carga, "in_system_context", return_value=True), \
             patch.object(carga, "leased_resource", self._leases([False])), \
             patch.object(carga.time, "sleep"):
            with carga.operacao_pesada_com_espera(timeout_s=0, poll_s=0) as conseguiu:
                self.assertFalse(conseguiu)

    def test_sqlite_nao_espera(self):
        """Em dev/testes não há processos concorrentes e a função nem existe."""
        from apps.scrapers import carga
        conexao = MagicMock()
        conexao.vendor = "sqlite"
        with patch.object(carga, "connections", {"default": conexao}):
            with carga.operacao_pesada_com_espera(timeout_s=999) as conseguiu:
                self.assertTrue(conseguiu)
        conexao.cursor.assert_not_called()

    def test_runtime_web_falha_fechado_sem_tocar_lease_global(self):
        from apps.scrapers import carga
        conexao = MagicMock(vendor="postgresql")
        with patch.object(carga, "connections", {"default": conexao}), \
             patch.object(carga, "in_system_context", return_value=False), \
             patch.object(carga, "leased_resource") as lease:
            with carga.operacao_pesada_com_espera() as conseguiu:
                self.assertFalse(conseguiu)
        lease.assert_not_called()
