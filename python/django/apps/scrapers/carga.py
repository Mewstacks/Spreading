"""Coordenação de carga pesada entre os processos de automação.

Os workers vivem no mesmo app hoje, mas usam processos distintos. Um lock de
advisory do PostgreSQL é compartilhado entre eles, liberado automaticamente se o
processo ou a conexão morrer e não exige tabela/migration adicional.
"""
from contextlib import contextmanager

from django.db import connections


# Constante estável, dentro do intervalo bigint assinado aceito pelo PostgreSQL.
_HEAVY_PIPELINE_LOCK = 7_894_421_073


@contextmanager
def operacao_pesada():
    """Cede ``True`` somente a um pipeline browser/escrita intensiva por vez."""
    connection = connections["default"]
    if connection.vendor != "postgresql":
        # SQLite é usado no desenvolvimento/testes; não há processos concorrentes
        # nessa configuração e ele não implementa pg_try_advisory_lock.
        yield True
        return

    acquired = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [_HEAVY_PIPELINE_LOCK])
            acquired = bool(cursor.fetchone()[0])
        yield acquired
    finally:
        if acquired:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [_HEAVY_PIPELINE_LOCK])
            except Exception:
                # A queda da conexão também libera advisory locks no servidor.
                pass
