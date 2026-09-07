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


def renovar_conexoes_antigas() -> None:
    """`close_old_connections` do Django, sem o tiro no pé dentro de transação.

    `close_old_connections()` chama `close_if_unusable_or_obsolete()`, e a PRIMEIRA
    coisa que esse método faz é comparar `get_autocommit()` com o `AUTOCOMMIT` do
    settings: se divergirem, ele fecha a conexão sem mais perguntas. Dentro de um
    `atomic()` eles SEMPRE divergem — é o que `atomic` faz — então a chamada não
    renova nada, fecha uma conexão que está no meio de uma transação e a deixa em
    `closed_in_transaction`.

    Isso não é teoria: `organization_callable` já documentava o efeito ("dentro de
    uma transação o Django NÃO fecha a conexão ... nenhuma tentativa de renovar
    (`close_all`, `close_old_connections`) tem efeito ali dentro") e mesmo assim
    abria com `close_old_connections()`. Em produção passava porque os chamadores
    de fora começam sem transação; sob o `TestCase`, que embrulha tudo num atomic,
    quebrava sempre.
    """
    from django.db import close_old_connections

    if any(conexao.in_atomic_block for conexao in connections.all()):
        return
    close_old_connections()
