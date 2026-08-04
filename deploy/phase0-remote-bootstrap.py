"""Bootstrap one-shot executado dentro da imagem Django antiga.

O material secreto vem apenas de PHASE0_DB_BOOTSTRAP_JSON. Este arquivo não
contém, imprime ou persiste credenciais.
"""

import base64
import json
import os
import sys

sys.path.insert(0, "/app/django")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

import django

django.setup()

from django.db import connection, transaction


encoded_credentials = os.environ["PHASE0_DB_BOOTSTRAP_B64"]
encoded_credentials += "=" * (-len(encoded_credentials) % 4)
credentials = json.loads(
    base64.urlsafe_b64decode(encoded_credentials).decode("utf-8")
)
roles = {
    "spreading_runtime": credentials["runtime"],
    "spreading_system": credentials["system"],
    "spreading_migration": credentials["migration"],
}


def ident(value):
    return connection.ops.quote_name(value)


with transaction.atomic():
    with connection.cursor() as cursor:
        for role, password in roles.items():
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
            if cursor.fetchone() is None:
                cursor.execute(
                    f"CREATE ROLE {ident(role)} LOGIN NOSUPERUSER "
                    "NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                )
            cursor.execute(
                f"ALTER ROLE {ident(role)} WITH LOGIN NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD %s",
                [password],
            )

        cursor.execute("SELECT current_database()")
        database_name = cursor.fetchone()[0]
        cursor.execute(
            f"ALTER DATABASE {ident(database_name)} OWNER TO spreading_migration"
        )
        cursor.execute("ALTER SCHEMA public OWNER TO spreading_migration")
        cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        cursor.execute(
            "REVOKE CREATE ON SCHEMA public "
            "FROM spreading_runtime, spreading_system"
        )

        cursor.execute(
            """
            SELECT c.relkind, c.relname
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public'
               AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
             ORDER BY CASE WHEN c.relkind IN ('r', 'p') THEN 0
                           WHEN c.relkind IN ('v', 'm') THEN 1 ELSE 2 END,
                      c.relname
            """
        )
        kind_sql = {
            "r": "TABLE",
            "p": "TABLE",
            "v": "VIEW",
            "m": "MATERIALIZED VIEW",
            "S": "SEQUENCE",
        }
        for kind, name in cursor.fetchall():
            cursor.execute(
                f"ALTER {kind_sql[kind]} {ident(name)} "
                "OWNER TO spreading_migration"
            )

        for role in (
            "spreading_runtime",
            "spreading_system",
            "spreading_migration",
        ):
            cursor.execute(
                f"GRANT CONNECT ON DATABASE {ident(database_name)} TO {ident(role)}"
            )
        cursor.execute(
            "GRANT USAGE ON SCHEMA public TO spreading_runtime, spreading_system"
        )
        cursor.execute(
            "GRANT USAGE, CREATE ON SCHEMA public TO spreading_migration"
        )
        cursor.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            "TO spreading_runtime, spreading_system"
        )
        cursor.execute(
            "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public "
            "TO spreading_runtime, spreading_system"
        )
        cursor.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE spreading_migration IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES "
            "TO spreading_runtime, spreading_system"
        )
        cursor.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE spreading_migration IN SCHEMA public "
            "GRANT USAGE, SELECT, UPDATE ON SEQUENCES "
            "TO spreading_runtime, spreading_system"
        )

        cursor.execute(
            """
            SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls
              FROM pg_roles
             WHERE rolname = ANY(%s)
            """,
            [list(roles)],
        )
        rows = cursor.fetchall()
        if len(rows) != 3 or any(any(row[1:]) for row in rows):
            raise RuntimeError(
                "As roles tenant não ficaram com privilégios mínimos."
            )

        cursor.execute(
            """
            SELECT count(*)
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_roles r ON r.oid = c.relowner
             WHERE n.nspname = 'public'
               AND c.relkind IN ('r', 'p')
               AND r.rolname <> 'spreading_migration'
            """
        )
        if cursor.fetchone()[0]:
            raise RuntimeError(
                "Há tabelas da aplicação fora da role de migração."
            )

print("Roles tenant provisionadas e verificadas sem expor credenciais.")
