import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.accounts.rls import (
    ALL_TENANT_TABLES,
    CONTROL_TENANT_TABLES,
    MIXED_TENANT_TABLES,
    policy_statements,
)


class Command(BaseCommand):
    help = "Habilita, desabilita ou inspeciona o RLS multi-tenant."

    def add_arguments(self, parser):
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--enable", action="store_true")
        action.add_argument("--disable", action="store_true")
        action.add_argument("--status", action="store_true")
        parser.add_argument(
            "--system-role", default=settings.TENANT_SYSTEM_DB_ROLE,
        )
        parser.add_argument(
            "--migration-role", default=settings.TENANT_MIGRATION_DB_ROLE,
        )

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            if options["status"]:
                self.stdout.write("RLS não aplicável: backend não é PostgreSQL.")
                return
            raise CommandError("RLS só pode ser alterado no PostgreSQL.")

        if options["status"]:
            self._status()
            return

        system_role = options["system_role"]
        migration_role = options["migration_role"]
        for role in (system_role, migration_role):
            if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
                raise CommandError(f"Nome de role inválido: {role!r}")

        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                [[system_role, migration_role]],
            )
            found_roles = {row[0] for row in cursor.fetchall()}
            missing_roles = {system_role, migration_role} - found_roles
            if options["enable"] and missing_roles:
                raise CommandError(
                    "Roles exigidas pelo RLS não existem: "
                    + ", ".join(sorted(missing_roles))
                )
            if options["enable"]:
                self._install_signed_context(
                    cursor,
                    system_role=system_role,
                    migration_role=migration_role,
                )
            for table in ALL_TENANT_TABLES:
                if options["enable"]:
                    for sql in policy_statements(
                        table,
                        mixed=table in MIXED_TENANT_TABLES,
                        system_role=system_role,
                        migration_role=migration_role,
                    ):
                        cursor.execute(sql)
                else:
                    cursor.execute(
                        f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY'
                    )
                    cursor.execute(
                        f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY'
                    )
        self.stdout.write(self.style.SUCCESS(
            f"RLS {'habilitado e forçado' if options['enable'] else 'desabilitado'} "
            f"em {len(ALL_TENANT_TABLES)} tabelas."
        ))

    def _install_signed_context(self, cursor, *, system_role, migration_role):
        """Instala o verificador HMAC sem conceder acesso ao segredo."""
        secret = settings.TENANT_CONTEXT_SIGNING_KEY
        if not secret:
            raise CommandError(
                "TENANT_CONTEXT_SIGNING_KEY é obrigatória para habilitar RLS."
            )

        runtime_role = settings.TENANT_RUNTIME_DB_ROLE
        for role in (runtime_role, system_role, migration_role):
            if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
                raise CommandError(f"Nome de role inválido: {role!r}")
        qn = connection.ops.quote_name

        cursor.execute("SELECT current_user")
        if cursor.fetchone()[0] != migration_role:
            raise CommandError(
                "O contexto RLS assinado só pode ser instalado pela role de migração."
            )

        # public deixa de ser gravável por roles não confiáveis antes de carregar
        # pgcrypto, conforme a recomendação de segurança de extensions/functions.
        cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        cursor.execute(
            f"REVOKE CREATE ON SCHEMA public FROM "
            f"{qn(runtime_role)}, {qn(system_role)}"
        )
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
        cursor.execute(
            """
            SELECT n.nspname
              FROM pg_extension e
              JOIN pg_namespace n ON n.oid = e.extnamespace
             WHERE e.extname = 'pgcrypto'
            """
        )
        row = cursor.fetchone()
        if not row or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", row[0]):
            raise CommandError("Schema do pgcrypto ausente ou inválido.")
        crypto_schema = row[0]
        cursor.execute(
            """
            SELECT has_schema_privilege(%s, %s, 'CREATE'),
                   has_schema_privilege(%s, %s, 'CREATE')
            """,
            [runtime_role, crypto_schema, system_role, crypto_schema],
        )
        if any(cursor.fetchone()):
            raise CommandError(
                "O schema do pgcrypto é gravável por uma role não confiável."
            )

        cursor.execute(
            f"CREATE SCHEMA IF NOT EXISTS tenant_security "
            f"AUTHORIZATION {qn(migration_role)}"
        )
        cursor.execute("REVOKE ALL ON SCHEMA tenant_security FROM PUBLIC")
        cursor.execute(
            f"GRANT USAGE ON SCHEMA tenant_security TO "
            f"{qn(runtime_role)}, {qn(system_role)}"
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tenant_security.context_secret (
                singleton boolean PRIMARY KEY DEFAULT TRUE
                    CHECK (singleton),
                secret text NOT NULL CHECK (length(secret) >= 43)
            )
            """
        )
        cursor.execute(
            """
            INSERT INTO tenant_security.context_secret (singleton, secret)
            VALUES (TRUE, %s)
            ON CONFLICT (singleton)
            DO UPDATE SET secret = EXCLUDED.secret
            """,
            [secret],
        )
        cursor.execute(
            f"REVOKE ALL ON tenant_security.context_secret FROM PUBLIC, "
            f"{qn(runtime_role)}, {qn(system_role)}"
        )

        # O segredo permanece em tabela sem grants. A policy chama somente este
        # verificador SECURITY DEFINER, com search_path fixo e HMAC qualificado.
        cursor.execute(
            f"""
            CREATE OR REPLACE FUNCTION tenant_security.context_valid(
                context_kind text,
                context_value text,
                supplied_signature text
            )
            RETURNS boolean
            LANGUAGE sql
            STABLE
            PARALLEL UNSAFE
            SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $tenant_context$
                SELECT CASE
                    WHEN supplied_signature ~ '^[0-9a-f]{{64}}$' THEN
                        pg_catalog.decode(supplied_signature, 'hex') =
                        {qn(crypto_schema)}.hmac(
                            pg_catalog.convert_to(
                                context_kind || ':' || context_value,
                                'UTF8'
                            ),
                            pg_catalog.convert_to(secret, 'UTF8'),
                            'sha256'
                        )
                    ELSE FALSE
                END
                FROM tenant_security.context_secret
                WHERE singleton = TRUE
            $tenant_context$
            """
        )
        cursor.execute(
            """
            REVOKE ALL ON FUNCTION
                tenant_security.context_valid(text, text, text)
            FROM PUBLIC
            """
        )
        cursor.execute(
            f"""
            GRANT EXECUTE ON FUNCTION
                tenant_security.context_valid(text, text, text)
            TO {qn(runtime_role)}, {qn(system_role)}, {qn(migration_role)}
            """
        )

    def _status(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = current_schema()
                   AND c.relname = ANY(%s)
                 ORDER BY c.relname
                """,
                [list(ALL_TENANT_TABLES)],
            )
            rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT tablename, policyname, cmd,
                       COALESCE(qual, ''), COALESCE(with_check, '')
                  FROM pg_policies
                 WHERE schemaname = current_schema()
                   AND tablename = ANY(%s)
                 ORDER BY tablename, policyname
                """,
                [list(ALL_TENANT_TABLES)],
            )
            policies = cursor.fetchall()
            cursor.execute(
                """
                SELECT p.prosecdef,
                       owner.rolname,
                       COALESCE(array_to_string(p.proconfig, ','), ''),
                       has_function_privilege(%s, p.oid, 'EXECUTE'),
                       has_function_privilege(%s, p.oid, 'EXECUTE'),
                       NOT EXISTS (
                           SELECT 1
                             FROM aclexplode(
                                 COALESCE(p.proacl, acldefault('f', p.proowner))
                             ) acl
                            WHERE acl.grantee = 0
                              AND acl.privilege_type = 'EXECUTE'
                       ),
                       pg_get_functiondef(p.oid)
                  FROM pg_proc p
                  JOIN pg_namespace n ON n.oid = p.pronamespace
                  JOIN pg_roles owner ON owner.oid = p.proowner
                 WHERE n.nspname = 'tenant_security'
                   AND p.proname = 'context_valid'
                   AND p.pronargs = 3
                """,
                [
                    settings.TENANT_RUNTIME_DB_ROLE,
                    settings.TENANT_SYSTEM_DB_ROLE,
                ],
            )
            context_function = cursor.fetchone()
            cursor.execute(
                """
                SELECT has_schema_privilege(%s, 'tenant_security', 'USAGE'),
                       has_schema_privilege(%s, 'tenant_security', 'CREATE'),
                       has_table_privilege(
                           %s,
                           'tenant_security.context_secret',
                           'SELECT'
                       ),
                       has_schema_privilege(%s, 'tenant_security', 'USAGE'),
                       has_table_privilege(
                           %s,
                           'tenant_security.context_secret',
                           'SELECT'
                       )
                """,
                [
                    settings.TENANT_RUNTIME_DB_ROLE,
                    settings.TENANT_RUNTIME_DB_ROLE,
                    settings.TENANT_RUNTIME_DB_ROLE,
                    settings.TENANT_SYSTEM_DB_ROLE,
                    settings.TENANT_SYSTEM_DB_ROLE,
                ],
            )
            context_privileges = cursor.fetchone()
        found = {name for name, _, _ in rows}
        for name, enabled, forced in rows:
            self.stdout.write(
                f"{name}: enabled={str(enabled).lower()} forced={str(forced).lower()}"
            )
        missing = sorted(set(ALL_TENANT_TABLES) - found)
        if missing:
            raise CommandError(f"Tabelas ausentes: {', '.join(missing)}")
        if any(not enabled or not forced for _, enabled, forced in rows):
            raise CommandError("RLS ainda não está ENABLE + FORCE em todas as tabelas.")

        by_table = {}
        for table, name, command, using, with_check in policies:
            by_table.setdefault(table, {})[name] = (
                command,
                using,
                with_check,
            )
        expected = {
            "tenant_select": "SELECT",
            "tenant_insert": "INSERT",
            "tenant_update": "UPDATE",
            "tenant_delete": "DELETE",
        }
        policy_errors = []
        for table in ALL_TENANT_TABLES:
            table_policies = by_table.get(table, {})
            if set(table_policies) != set(expected):
                policy_errors.append(f"{table}: conjunto de policies divergente")
                continue
            for name, command in expected.items():
                actual_command, using, with_check = table_policies[name]
                expression = f"{using} {with_check}"
                if actual_command != command:
                    policy_errors.append(f"{table}.{name}: comando divergente")
                if (
                    "app.organization_id" not in expression
                    or "app.organization_signature" not in expression
                    or "app.system_context" not in expression
                    or "app.system_signature" not in expression
                    or "tenant_security.context_valid" not in expression
                    or "CURRENT_USER" not in expression.upper()
                ):
                    policy_errors.append(
                        f"{table}.{name}: expressão fail-closed ausente"
                    )
            select_expression = " ".join(table_policies["tenant_select"][1:])
            if table in CONTROL_TENANT_TABLES and (
                "app.actor_id" not in select_expression
                or "app.actor_signature" not in select_expression
            ):
                policy_errors.append(
                    f"{table}.tenant_select: contexto de ator assinado ausente"
                )
            public_visible = "organization_id IS NULL" in select_expression
            if public_visible != (table in MIXED_TENANT_TABLES):
                policy_errors.append(
                    f"{table}.tenant_select: visibilidade pública divergente"
                )
        if policy_errors:
            raise CommandError(
                "Policies RLS inválidas: " + "; ".join(policy_errors[:8])
            )
        if not context_function:
            raise CommandError("Função de contexto RLS assinado ausente.")
        (
            security_definer,
            function_owner,
            function_config,
            runtime_execute,
            system_execute,
            public_revoked,
            function_definition,
        ) = context_function
        if (
            not security_definer
            or function_owner != settings.TENANT_MIGRATION_DB_ROLE
            or "search_path=pg_catalog, pg_temp" not in function_config
            or not runtime_execute
            or not system_execute
            or not public_revoked
            or ".hmac(" not in function_definition
            or "tenant_security.context_secret" not in function_definition
        ):
            raise CommandError("Função de contexto RLS assinado insegura.")
        (
            runtime_usage,
            runtime_create,
            runtime_secret_read,
            system_usage,
            system_secret_read,
        ) = context_privileges
        if (
            not runtime_usage
            or runtime_create
            or runtime_secret_read
            or not system_usage
            or system_secret_read
        ):
            raise CommandError(
                "Privilégios do segredo de contexto RLS estão inseguros."
            )
        self.stdout.write(self.style.SUCCESS(
            "Contexto tenant assinado por HMAC e segredo inacessível às roles "
            "runtime/system."
        ))
