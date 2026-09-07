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
