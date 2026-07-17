import json
import os
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
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
    def test_amazon_session_is_isolated_per_user_and_readable_only_temporarily(self):
        from apps.scrapers.report_sessions import (
            decrypted_state_file, encrypted_state_path, has_report_session, save_report_state,
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
            with decrypted_state_file(first, "amazon") as temporary:
                with open(temporary, encoding="utf-8") as handle:
                    self.assertEqual(json.load(handle), state)
            self.assertFalse(os.path.exists(temporary))

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
