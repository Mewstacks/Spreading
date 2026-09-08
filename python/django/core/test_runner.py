"""Runner que devolve o banco de teste ao final.

`DROP DATABASE` falhava com `database "test_spreading" is being accessed by other
users` mesmo com a suíte inteira verde. A sessão sobrevivente é a do executor de
ORM (`apps.accounts.tenant._obter_executor_orm`): uma thread persistente pelo
processo, que existe justamente para manter UMA conexão aberta e não pagar TLS
novo a cada gravação fora do event loop.

Em produção esse executor deve mesmo viver enquanto o processo viver. Quem precisa
encerrá-lo é quem vai derrubar o banco — e isso é o runner, não o código de
produção. Amostrar `pg_stat_activity` durante a suíte foi o que apontou o PID, com
`set_config('app.organization_id', '', false)` como última query.
"""
from django.test.runner import DiscoverRunner


class SpreadingTestRunner(DiscoverRunner):
    def teardown_databases(self, old_config, **kwargs):
        from apps.accounts.tenant import encerrar_executor_orm

        encerrar_executor_orm()
        super().teardown_databases(old_config, **kwargs)
