from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection, transaction

from apps.accounts.rls import CONTROL_TENANT_TABLES, STRICT_TENANT_TABLES
from apps.accounts.tenant import _context_signature, organization_context


class Command(BaseCommand):
    help = (
        "Prova, com a role runtime, que GUCs falsificados não atravessam tenants "
        "e que o segredo HMAC não pode ser lido."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization-id")

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("O probe de isolamento exige PostgreSQL.")

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT current_user, rolsuper, rolbypassrls
                  FROM pg_roles
                 WHERE rolname = current_user
                """
            )
            role, superuser, bypass_rls = cursor.fetchone()
            if (
                role != settings.TENANT_RUNTIME_DB_ROLE
                or superuser
                or bypass_rls
            ):
                raise CommandError(
                    "O probe deve rodar exclusivamente com a role runtime mínima."
                )
        organization_id = (
            options.get("organization_id")
            or "00000000-0000-0000-0000-000000000001"
        )

        expected_signature = _context_signature(
            "organization", organization_id,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT tenant_security.context_valid(
                           'organization', %s, %s
                       ),
                       tenant_security.context_valid(
                           'organization', %s, %s
                       )
                """,
                [
                    organization_id,
                    expected_signature,
                    organization_id,
                    "0" * 64,
                ],
            )
            valid_accepted, forged_accepted = cursor.fetchone()
        if not valid_accepted or forged_accepted:
            raise CommandError("O verificador HMAC não está fail-closed.")

        self._assert_forged_context_sees_nothing(
            organization_id=organization_id,
            system=False,
        )
        self._assert_forged_context_sees_nothing(
            organization_id="",
            system=True,
        )

        # O contexto legítimo pode ver somente linhas com o próprio UUID.
        with organization_context(organization_id):
            with connection.cursor() as cursor:
                for table in STRICT_TENANT_TABLES:
                    cursor.execute(
                        f'SELECT count(*) FROM "{table}" '
                        "WHERE organization_id <> %s",
                        [organization_id],
                    )
                    if cursor.fetchone()[0]:
                        raise CommandError(
                            f"Contexto legítimo atravessou tenant em {table}."
                        )
                cursor.execute(
                    """
                    SELECT count(*) FROM accounts_organization
                    WHERE id <> %s
                    """,
                    [organization_id],
                )
                if cursor.fetchone()[0]:
                    raise CommandError(
                        "Contexto legítimo atravessou tenant em Organization."
                    )
                cursor.execute(
                    """
                    SELECT count(*) FROM accounts_membership
                    WHERE organization_id <> %s
                    """,
                    [organization_id],
                )
                if cursor.fetchone()[0]:
                    raise CommandError(
                        "Contexto legítimo atravessou tenant em Membership."
                    )

        secret_read_blocked = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT secret FROM tenant_security.context_secret"
                )
        except DatabaseError:
            secret_read_blocked = True
            connection.close()
        if not secret_read_blocked:
            raise CommandError("A role runtime conseguiu ler o segredo HMAC.")

        self.stdout.write(self.style.SUCCESS(
            "Probe aprovado: UUID/GUC forjado vê zero linhas privadas; contexto "
            "legítimo não cruza organização; segredo HMAC é inacessível."
        ))

    def _assert_forged_context_sees_nothing(
        self, *, organization_id: str, system: bool,
    ):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT set_config('app.organization_id', %s, true),
                       set_config('app.organization_signature', %s, true),
                       set_config('app.system_context', %s, true),
                       set_config('app.system_signature', %s, true),
                       set_config('app.actor_id', %s, true),
                       set_config('app.actor_signature', %s, true)
                """,
                [
                    organization_id,
                    "0" * 64,
                    "on" if system else "off",
                    "0" * 64,
                    "1",
                    "0" * 64,
                ],
            )
            for table in STRICT_TENANT_TABLES + CONTROL_TENANT_TABLES:
                cursor.execute(f'SELECT count(*) FROM "{table}"')
                if cursor.fetchone()[0]:
                    kind = "system" if system else "organization"
                    raise CommandError(
                        f"Contexto {kind} forjado enxergou linhas em {table}."
                    )
