import re

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.accounts.rls import STRICT_TENANT_TABLES


SCOPE_TABLES = (
    "scrapers_produto",
    "scrapers_cupomnormalizado",
)


def _constraint_name(table, suffix):
    name = f"{table}_{suffix}"
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"Nome de constraint inválido: {name}")
    return name


class Command(BaseCommand):
    help = (
        "Instala CHECKs NOT VALID sem reescrever tabela; valida em uma segunda onda."
    )

    def add_arguments(self, parser):
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--install", action="store_true")
        action.add_argument("--validate", action="store_true")
        action.add_argument("--ensure", action="store_true")
        action.add_argument("--status", action="store_true")
        action.add_argument("--drop", action="store_true")

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            if options["status"]:
                self.stdout.write(
                    "Constraints tenant não aplicáveis: backend não é PostgreSQL."
                )
                return
            raise CommandError("Constraints tenant operacionais exigem PostgreSQL.")

        constraints = [
            (
                table,
                _constraint_name(table, "organization_required"),
                "organization_id IS NOT NULL",
            )
            for table in STRICT_TENANT_TABLES
        ] + [
            (
                table,
                _constraint_name(table, "data_scope_coherent"),
                "("
                "(data_scope = 'public' AND organization_id IS NULL) OR "
                "(data_scope = 'organization' AND organization_id IS NOT NULL)"
                ")",
            )
            for table in SCOPE_TABLES
        ]

        if options["status"]:
            self._status(constraints)
            return

        with transaction.atomic(), connection.cursor() as cursor:
            for table, name, expression in constraints:
                if options["install"]:
                    cursor.execute(
                        f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}"'
                    )
                    cursor.execute(
                        f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" '
                        f"CHECK ({expression}) NOT VALID"
                    )
                elif options["validate"]:
                    cursor.execute(
                        f'ALTER TABLE "{table}" VALIDATE CONSTRAINT "{name}"'
                    )
                elif options["ensure"]:
                    cursor.execute(
                        """
                        SELECT convalidated
                          FROM pg_constraint con
                          JOIN pg_class rel ON rel.oid = con.conrelid
                          JOIN pg_namespace n ON n.oid = rel.relnamespace
                         WHERE n.nspname = current_schema()
                           AND rel.relname = %s
                           AND con.conname = %s
                        """,
                        [table, name],
                    )
                    row = cursor.fetchone()
                    if row is None:
                        cursor.execute(
                            f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" '
                            f"CHECK ({expression}) NOT VALID"
                        )
                        validated = False
                    else:
                        validated = row[0]
                    if not validated:
                        cursor.execute(
                            f'ALTER TABLE "{table}" '
                            f'VALIDATE CONSTRAINT "{name}"'
                        )
                else:
                    cursor.execute(
                        f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}"'
                    )
        verb = (
            "instaladas (NOT VALID)"
            if options["install"]
            else (
                "validadas"
                if options["validate"]
                else "garantidas e validadas"
                if options["ensure"]
                else "removidas"
            )
        )
        self.stdout.write(self.style.SUCCESS(
            f"{len(constraints)} constraints tenant {verb}."
        ))

    def _status(self, constraints):
        names = [name for _, name, _ in constraints]
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT conname, convalidated
                  FROM pg_constraint
                 WHERE conname = ANY(%s)
                 ORDER BY conname
                """,
                [names],
            )
            rows = cursor.fetchall()
        found = {name for name, _ in rows}
        for name, validated in rows:
            self.stdout.write(
                f"{name}: validated={str(validated).lower()}"
            )
        missing = sorted(set(names) - found)
        if missing:
            raise CommandError("Constraints ausentes: " + ", ".join(missing))
        if any(not validated for _, validated in rows):
            raise CommandError("Há constraints tenant ainda não validadas.")
