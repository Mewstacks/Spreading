"""Renovar conexão não pode matar a transação de quem chamou.

`connections.close_all()` dentro de um `atomic()` aberto não renova nada: aborta a
transação e deixa a conexão em `closed_in_transaction`, de onde o Django não
reabre. Toda query seguinte levanta `the connection is closed` — e essa mensagem
cobre o erro que apareceu primeiro.

Em 07/09/2026 isso derrubou 70 testes da suíte contra PostgreSQL de uma vez: o
`TestCase` embrulha cada teste num `atomic()`, e o `close_all()` incondicional no
topo de `_persistir_campanhas_cupons` matava a conexão antes da primeira query.
Em produção o mesmo tiro existe — só é raro, porque hoje os laços chamam isto
fora de transação.
"""
from unittest.mock import Mock

from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase

from apps.scrapers.db_conexao import renovar_conexoes


class ConexaoSobrevivemADentroDeTransacaoTests(TestCase):
    def test_dentro_de_atomic_a_conexao_continua_utilizavel(self):
        # O próprio TestCase já abriu um atomic; é exatamente o caso de produção
        # em que um chamador chama a raspagem de dentro de uma transação.
        self.assertTrue(connection.in_atomic_block)

        renovar_conexoes()

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_atomic_aninhado_tambem(self):
        with transaction.atomic():
            renovar_conexoes()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                self.assertEqual(cursor.fetchone()[0], 1)


class ForaDeTransacaoAindaDescartaTests(SimpleTestCase):
    """O motivo de existir continua valendo: socket ocioso tem de cair."""

    def test_fecha_a_conexao_ociosa(self):
        viva = Mock(in_atomic_block=False)
        ocupada = Mock(in_atomic_block=True)

        from apps.scrapers import db_conexao
        original = db_conexao.connections
        try:
            db_conexao.connections = Mock(all=Mock(return_value=[viva, ocupada]))
            db_conexao.renovar_conexoes()
        finally:
            db_conexao.connections = original

        viva.close.assert_called_once_with()
        ocupada.close.assert_not_called()


class ConexoesAntigasRespeitamTransacaoTests(TestCase):
    """`close_old_connections` é o mesmo tiro, por um caminho diferente.

    Ele compara `get_autocommit()` com o `AUTOCOMMIT` do settings e fecha quando
    divergem — e dentro de um `atomic()` eles sempre divergem. `organization_callable`
    abria e fechava com ele, e é o caminho que todo job de tenant atravessa.
    """

    def test_dentro_de_atomic_a_conexao_continua_utilizavel(self):
        from apps.scrapers.db_conexao import renovar_conexoes_antigas

        self.assertTrue(connection.in_atomic_block)

        renovar_conexoes_antigas()

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone()[0], 1)

    def test_o_callable_de_tenant_nao_derruba_a_transacao_de_quem_chamou(self):
        from apps.accounts.models import ensure_personal_organization
        from apps.accounts.tenant import organization_callable
        from django.contrib.auth.models import User

        usuario = User.objects.create_user("dono-da-transacao", password="x")
        organizacao = ensure_personal_organization(usuario)

        def contar():
            return User.objects.count()

        self.assertEqual(organization_callable(organizacao, contar)(), 1)
        # E a conexão de quem chamou segue de pé depois disso.
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone()[0], 1)


class RenovacaoEPorConexaoNaoTudoOuNadaTests(SimpleTestCase):
    """Pular TODAS quando qualquer uma está em transação vaza a outra.

    Foi o erro da primeira tentativa: os wrappers de thread existem justamente
    para devolver a conexão que a thread abriu, e um guard tudo-ou-nada deixava
    ela pendurada sempre que a thread principal estivesse num `atomic()` — que,
    sob `TestCase`, é sempre.
    """

    def test_fecha_a_ociosa_e_preserva_a_que_esta_em_transacao(self):
        from apps.scrapers import db_conexao

        em_transacao = Mock(in_atomic_block=True)
        ociosa = Mock(in_atomic_block=False)

        original = db_conexao.connections
        try:
            db_conexao.connections = Mock(
                all=Mock(return_value=[em_transacao, ociosa]))
            db_conexao.renovar_conexoes_antigas()
        finally:
            db_conexao.connections = original

        ociosa.close_if_unusable_or_obsolete.assert_called_once_with()
        em_transacao.close_if_unusable_or_obsolete.assert_not_called()

    def test_o_mesmo_vale_do_lado_de_accounts(self):
        """`tenant.py` repete a regra por causa do import circular."""
        from apps.accounts import tenant

        em_transacao = Mock(in_atomic_block=True)
        ociosa = Mock(in_atomic_block=False)

        original = tenant.connections
        try:
            tenant.connections = Mock(
                all=Mock(return_value=[em_transacao, ociosa]))
            tenant._renovar_conexoes_antigas()
        finally:
            tenant.connections = original

        ociosa.close_if_unusable_or_obsolete.assert_called_once_with()
        em_transacao.close_if_unusable_or_obsolete.assert_not_called()
