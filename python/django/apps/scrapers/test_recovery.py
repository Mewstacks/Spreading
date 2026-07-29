import tempfile
from datetime import timedelta
from unittest.mock import patch
from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.db import OperationalError
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
            encrypted_state_path, has_report_session, load_report_state,
            save_report_state,
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
            persisted = encrypted_state_path(first, "amazon").read_bytes()
            self.assertNotIn(b"opaque", persisted)

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

        rows = _parse_delimited_report(
            "Comissão;Etiqueta;Data;Cliques;Receita;Pedidos\n"
            "R$ 12,50;grupo-casa;17/07/2026;9;R$ 199,90;2\n".encode(),
            "amazon", timezone.localdate(), timezone.localdate(),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].etiqueta, "grupo-casa")
        self.assertEqual(rows[0].cliques, 9)
        self.assertEqual(rows[0].pedidos, 2)
        self.assertEqual(rows[0].receita, 199.90)
        self.assertEqual(rows[0].comissao, 12.50)


class HeavyPipelineLockTests(SimpleTestCase):
    @patch("apps.scrapers.carga.connections")
    def test_postgres_lock_releases_only_when_acquired(self, connections):
        from apps.scrapers.carga import operacao_pesada

        connection = connections.__getitem__.return_value
        connection.vendor = "postgresql"
        cursor = MagicMock()
        cursor.fetchone.return_value = (True,)
        connection.cursor.return_value.__enter__.return_value = cursor

        with operacao_pesada() as acquired:
            self.assertTrue(acquired)

        self.assertEqual(cursor.execute.call_count, 2)
        self.assertIn("pg_try_advisory_lock", cursor.execute.call_args_list[0].args[0])
        self.assertIn("pg_advisory_unlock", cursor.execute.call_args_list[1].args[0])


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
    """O botão manual disputa o MESMO advisory lock do worker. Sem isso, clicar
    enquanto o worker está no Link Builder abria um segundo Chromium no mesmo
    portal SSO com a mesma sessão — e o ML derrubava um dos dois para login."""

    def _conexao(self, respostas):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [(r,) for r in respostas]
        conexao = MagicMock()
        conexao.vendor = "postgresql"
        conexao.cursor.return_value.__enter__.return_value = cursor
        return conexao, cursor

    def test_espera_e_consegue_na_terceira(self):
        from apps.scrapers import carga
        conexao, cursor = self._conexao([False, False, True])
        avisos = []
        with patch.object(carga, "connections", {"default": conexao}), \
             patch.object(carga.time, "sleep"):
            with carga.operacao_pesada_com_espera(
                    poll_s=0, aviso=avisos.append) as conseguiu:
                self.assertTrue(conseguiu)
        self.assertEqual(len(avisos), 2)
        sqls = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertEqual(sum("pg_advisory_unlock" in s for s in sqls), 1)

    def test_timeout_nao_adquire_e_nao_desbloqueia(self):
        """Soltar um lock que não é nosso derrubaria o worker no meio do lote."""
        from apps.scrapers import carga
        conexao, cursor = self._conexao([False] * 20)
        with patch.object(carga, "connections", {"default": conexao}), \
             patch.object(carga.time, "sleep"):
            with carga.operacao_pesada_com_espera(timeout_s=0, poll_s=0) as conseguiu:
                self.assertFalse(conseguiu)
        sqls = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertFalse(any("pg_advisory_unlock" in s for s in sqls))

    def test_sqlite_nao_espera(self):
        """Em dev/testes não há processos concorrentes e a função nem existe."""
        from apps.scrapers import carga
        conexao = MagicMock()
        conexao.vendor = "sqlite"
        with patch.object(carga, "connections", {"default": conexao}):
            with carga.operacao_pesada_com_espera(timeout_s=999) as conseguiu:
                self.assertTrue(conseguiu)
        conexao.cursor.assert_not_called()
