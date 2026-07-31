"""Coordenação de carga pesada entre os processos de automação.

Os workers vivem no mesmo app hoje, mas usam processos distintos. Um lock de
advisory do PostgreSQL é compartilhado entre eles, liberado automaticamente se o
processo ou a conexão morrer e não exige tabela/migration adicional.
"""
import time
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


@contextmanager
def operacao_pesada_com_espera(timeout_s=150, poll_s=5, aviso=None):
    """Como ``operacao_pesada``, mas ESPERA a vez em vez de desistir na hora.

    É o mesmo lock e o mesmo recurso: o objetivo é justamente disputar com o worker
    de links, não criar uma fila paralela.

    Existe para o clique manual em "Gerar links de afiliado". Sem disputar o lock,
    o botão abria um SEGUNDO Chromium no portal de afiliados enquanto o worker já
    estava lá com a MESMA sessão — e o SSO do Mercado Livre derrubava um dos dois
    para a tela de login. O sintoma era "sessão expirada" numa conta perfeitamente
    conectada.

    ``aviso(segundos_decorridos)`` é chamado a cada tentativa frustrada, para o
    chamador informar a espera (no SSE, um ``print``). Devolve ``False`` no timeout:
    não é erro — o worker pega esses produtos no ciclo dele.
    """
    connection = connections["default"]
    if connection.vendor != "postgresql":
        # Mesmo motivo de operacao_pesada: em SQLite (dev/testes) não há
        # concorrência entre processos e a função do Postgres não existe.
        yield True
        return

    def _tentar():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [_HEAVY_PIPELINE_LOCK])
            return bool(cursor.fetchone()[0])

    acquired = False
    inicio = time.monotonic()
    try:
        while True:
            acquired = _tentar()
            se_passou = time.monotonic() - inicio
            if acquired or se_passou >= timeout_s:
                break
            if aviso is not None:
                aviso(int(se_passou))
            time.sleep(poll_s)
        yield acquired
    finally:
        if acquired:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", [_HEAVY_PIPELINE_LOCK])
            except Exception:
                # A queda da conexão também libera advisory locks no servidor.
                pass
