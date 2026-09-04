"""Definição central das tabelas protegidas por RLS."""

import re

STRICT_TENANT_TABLES = (
    "accounts_perfil",
    "accounts_mercadolivresession",
    "accounts_browsersession",
    "accounts_whatsappconnection",
    "accounts_organizationfeatureoverride",
    "scrapers_integracaoafiliado",
    "scrapers_programaafiliado",
    "scrapers_historicoenvio",
    "scrapers_publicacao",
    "scrapers_cliquepublicacao",
    "scrapers_linkafiliadocupomusuario",
    "scrapers_linkafiliadoprodutocupomusuario",
    "scrapers_receitaafiliado",
    "scrapers_relatoriosync",
    "scrapers_linkafiliadousuario",
    "scrapers_canalmonitorado",
    "scrapers_enviocanal",
    "scrapers_configuracaoenvio",
    "scrapers_execucaoraspagem",
    "scrapers_eventoraspagem",
    "scrapers_cupomdisponibilidade",
    "scrapers_cupomdisponibilidadeevento",
    "scrapers_cupomvalidacao",
    "scrapers_publicacaotentativa",
    "scrapers_publicacaoevento",
)

MIXED_TENANT_TABLES = (
    "scrapers_produto",
    "scrapers_cupomnormalizado",
    "scrapers_cupompreparacao",
    "scrapers_produtocupom",
    "scrapers_execucaoingestao",
    "scrapers_eventooperacional",
    "scrapers_incidentesaude",
    "scrapers_cupomfonteobservacao",
)

SYSTEM_ONLY_TABLES = (
    "scrapers_workerheartbeat",
    "scrapers_resourcelease",
)

CONTROL_TENANT_TABLES = (
    "accounts_organization",
    "accounts_membership",
)

ALL_TENANT_TABLES = (
    STRICT_TENANT_TABLES + MIXED_TENANT_TABLES + CONTROL_TENANT_TABLES
    + SYSTEM_ONLY_TABLES
)


def organization_expr(column: str = "organization_id") -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_.]{0,127}", column):
        raise ValueError(f"Coluna de organização inválida: {column!r}")
    return (
        f"({column} = "
        "NULLIF(current_setting('app.organization_id', true), '')::uuid "
        "AND (SELECT tenant_security.context_valid("
        "'organization', "
        "current_setting('app.organization_id', true), "
        "current_setting('app.organization_signature', true)"
        ")))"
    )


ORG_EXPR = organization_expr()

ACTOR_EXPR = (
    "(user_id = "
    "NULLIF(current_setting('app.actor_id', true), '')::bigint "
    "AND (SELECT tenant_security.context_valid("
    "'actor', "
    "current_setting('app.actor_id', true), "
    "current_setting('app.actor_signature', true)"
    ")))"
)

ORGANIZATION_ACTOR_EXPR = (
    "((accounts_organization.personal_owner_id = "
    "NULLIF(current_setting('app.actor_id', true), '')::bigint "
    "OR EXISTS ("
    "SELECT 1 FROM accounts_membership tenant_actor_membership "
    "WHERE tenant_actor_membership.organization_id = accounts_organization.id "
    "AND tenant_actor_membership.user_id = "
    "NULLIF(current_setting('app.actor_id', true), '')::bigint "
    "AND tenant_actor_membership.is_active"
    ")) "
    "AND (SELECT tenant_security.context_valid("
    "'actor', "
    "current_setting('app.actor_id', true), "
    "current_setting('app.actor_signature', true)"
    ")))"
)


def _role_literal(role: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", str(role or "")):
        raise ValueError(f"Nome de role PostgreSQL inválido: {role!r}")
    return f"'{role}'"


def system_expr(system_role: str, migration_role: str) -> str:
    roles = ", ".join(map(_role_literal, (system_role, migration_role)))
    return (
        "current_setting('app.system_context', true) = 'on' "
        f"AND current_user IN ({roles}) "
        "AND (SELECT tenant_security.context_valid("
        "'system', '', current_setting('app.system_signature', true)"
        "))"
    )


def policy_statements(
    table: str,
    *,
    mixed: bool,
    system_only: bool = False,
    system_role: str = "spreading_system",
    migration_role: str = "spreading_migration",
) -> list[str]:
    system = system_expr(system_role, migration_role)
    if system_only:
        visible = writable = f"({system})"
    elif table == "accounts_organization":
        visible = (
            f"(({system}) OR "
            f"{organization_expr('accounts_organization.id')} OR "
            f"{ORGANIZATION_ACTOR_EXPR})"
        )
        writable = (
            f"(({system}) OR "
            f"{organization_expr('accounts_organization.id')})"
        )
    elif table == "accounts_membership":
        visible = f"(({system}) OR {ORG_EXPR} OR {ACTOR_EXPR})"
        writable = f"(({system}) OR {ORG_EXPR})"
    elif table == "accounts_perfil":
        visible = f"(({system}) OR {ORG_EXPR} OR {ACTOR_EXPR})"
        # Perfil continua fisicamente ligado à organização pessoal durante a
        # compatibilidade, mesmo quando o usuário atua em uma organização
        # compartilhada. O actor assinado pode ler/escrever somente a própria linha;
        # a validação de active_organization ainda exige Membership ativa.
        writable = f"(({system}) OR {ORG_EXPR} OR {ACTOR_EXPR})"
    else:
        # O catálogo compartilhado é quase todo público. Colocá-lo primeiro
        # evita até o InitPlan de assinatura para essas linhas; para as privadas,
        # cada assinatura continua obrigatória e validada pelo mesmo HMAC.
        visible = "(organization_id IS NULL OR " if mixed else "("
        visible += f"({system}) OR {ORG_EXPR}"
        visible += ")"
        writable = f"(({system}) OR {ORG_EXPR})"
    return [
        f'DROP POLICY IF EXISTS tenant_select ON "{table}"',
        f'DROP POLICY IF EXISTS tenant_insert ON "{table}"',
        f'DROP POLICY IF EXISTS tenant_update ON "{table}"',
        f'DROP POLICY IF EXISTS tenant_delete ON "{table}"',
        (
            f'CREATE POLICY tenant_select ON "{table}" FOR SELECT '
            f"USING {visible}"
        ),
        (
            f'CREATE POLICY tenant_insert ON "{table}" FOR INSERT '
            f"WITH CHECK {writable}"
        ),
        (
            f'CREATE POLICY tenant_update ON "{table}" FOR UPDATE '
            f"USING {writable} WITH CHECK {writable}"
        ),
        (
            f'CREATE POLICY tenant_delete ON "{table}" FOR DELETE '
            f"USING {writable}"
        ),
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY',
    ]
