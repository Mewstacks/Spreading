from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from apps.accounts.rls import ALL_TENANT_TABLES


class Command(BaseCommand):
    help = (
        "Falha se a role conectada puder contornar/desabilitar o RLS. "
        "Use nas máquinas web e workers, nunca no release command."
    )

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("A auditoria da role exige PostgreSQL.")

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT r.rolname, r.rolsuper, r.rolbypassrls
                  FROM pg_roles r
                 WHERE r.rolname = current_user
                """
            )
            role, is_superuser, bypass_rls = cursor.fetchone()
            cursor.execute(
                """
                SELECT c.relname
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  JOIN pg_roles r ON r.oid = c.relowner
                 WHERE n.nspname = current_schema()
                   AND c.relname = ANY(%s)
                   AND r.rolname = current_user
                 ORDER BY c.relname
                """,
                [list(ALL_TENANT_TABLES)],
            )
            owned = [row[0] for row in cursor.fetchall()]

        problems = []
        expected_role = (
            settings.TENANT_MIGRATION_DB_ROLE
            if settings.RELEASE_COMMAND_PROCESS
            else (
                settings.TENANT_SYSTEM_DB_ROLE
                if settings.TENANT_SYSTEM_PROCESS
                else settings.TENANT_RUNTIME_DB_ROLE
            )
        )
        if role != expected_role:
            problems.append(
                f"role atual é {role!r}, mas o processo exige {expected_role!r}"
            )
        if is_superuser:
            problems.append("é SUPERUSER")
        if bypass_rls:
            problems.append("tem BYPASSRLS")
        if settings.RELEASE_COMMAND_PROCESS:
            missing_owned = sorted(set(ALL_TENANT_TABLES) - set(owned))
            if missing_owned:
                problems.append(
                    "não é dona de tabela(s) tenant: "
                    + ", ".join(missing_owned[:5])
                    + ("..." if len(missing_owned) > 5 else "")
                )
        elif owned:
            problems.append(
                "é dona de tabela(s) tenant: " + ", ".join(owned[:5])
                + ("..." if len(owned) > 5 else "")
            )
        if problems:
            raise CommandError(
                f"Role de banco {role!r} não é segura: " + "; ".join(problems)
            )
        self.stdout.write(self.style.SUCCESS(
            f"Role {role!r}: identidade correta, sem SUPERUSER, BYPASSRLS "
            + (
                "e com ownership de migração."
                if settings.RELEASE_COMMAND_PROCESS
                else "ou ownership tenant."
            )
        ))
