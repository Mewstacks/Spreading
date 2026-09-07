"""Renovar a conexão do banco sem destruir a transação de quem chamou.

Os laços longos precisam descartar o socket do Postgres de vez em quando: entre a
coleta (minutos de browser) e a gravação, o proxy da Fly derruba a conexão ociosa
sem o Django saber, e a primeira query do save estoura
``OperationalError("server closed the connection unexpectedly")``.

O jeito antigo era ``connections.close_all()`` puro. Ele está certo no topo de um
laço e errado em qualquer ponto que possa estar dentro de uma transação: fechar a
conexão no meio de um ``atomic()`` não renova nada — aborta o que estava aberto e
marca a conexão como ``closed_in_transaction``, estado do qual o Django **não**
reabre. Dali em diante toda query levanta ``the connection is closed``, e o erro
que apareceu primeiro some atrás desse.

Foi assim que a suíte contra PostgreSQL caiu em 07/09/2026: o ``TestCase`` do
Django embrulha cada teste num ``atomic()``, então o ``close_all()`` incondicional
no topo de ``_persistir_campanhas_cupons`` matava a conexão antes da primeira
query — 70 erros, todos com a mesma mensagem e nenhum com a causa. Em produção o
mesmo tiro existe, só que raro: basta um chamador já estar dentro de ``atomic()``.

`apps/accounts/tenant.py` já registrava a regra em `organization_callable_isolada`
("o fechamento fica FORA ... onde fechar a conexão a quebraria"). Isto aqui é a
mesma regra, disponível para os laços.
"""
from django.db import connections


def renovar_conexoes() -> None:
    """Fecha as conexões ociosas; pula as que estão dentro de uma transação."""
    for conexao in connections.all():
        if conexao.in_atomic_block:
            continue
        conexao.close()
