"""Contexto fail-closed de organização para HTTP, serviços e workers."""

import hashlib
import hmac
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import close_old_connections, connection, transaction
from django.db.backends.signals import connection_created
from django.dispatch import receiver


_current_organization_id = ContextVar("current_organization_id", default=None)
_current_actor_id = ContextVar("current_actor_id", default=None)
_system_context = ContextVar("tenant_system_context", default=False)


def current_organization_id():
    return _current_organization_id.get()


def current_actor_id():
    return _current_actor_id.get()


def in_system_context() -> bool:
    return bool(_system_context.get())


def _context_signature(kind: str, value: str = "") -> str:
    key = settings.TENANT_CONTEXT_SIGNING_KEY
    if not key:
        if settings.APP_ENV in {"staging", "production"}:
            raise ImproperlyConfigured(
                "TENANT_CONTEXT_SIGNING_KEY é obrigatória para o RLS assinado."
            )
        return ""
    message = f"{kind}:{value}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _set_postgres_context(*, organization_id=None, system=False, local=True):
    if connection.vendor != "postgresql":
        return
    organization_value = str(organization_id or "")
    organization_signature = (
        _context_signature("organization", organization_value)
        if organization_value and not system
        else ""
    )
    system_signature = _context_signature("system") if system else ""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT set_config('app.organization_id', %s, %s),
                   set_config('app.organization_signature', %s, %s),
                   set_config('app.system_context', %s, %s),
                   set_config('app.system_signature', %s, %s)
            """,
            [
                organization_value,
                local,
                organization_signature,
                local,
                "on" if system else "off",
                local,
                system_signature,
                local,
            ],
        )


def _set_postgres_actor(actor_id=None, *, local=True):
    if connection.vendor != "postgresql":
        return
    actor_value = str(actor_id or "")
    signature = (
        _context_signature("actor", actor_value) if actor_value else ""
    )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT set_config('app.actor_id', %s, %s),
                   set_config('app.actor_signature', %s, %s)
            """,
            [actor_value, local, signature, local],
        )


def _assert_privileged_database_role():
    if connection.vendor != "postgresql":
        return
    from django.conf import settings
    from django.core.exceptions import PermissionDenied

    allowed = {
        settings.TENANT_SYSTEM_DB_ROLE,
        settings.TENANT_MIGRATION_DB_ROLE,
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT current_user, r.rolsuper, r.rolbypassrls
              FROM pg_roles r
             WHERE r.rolname = current_user
            """
        )
        current, is_superuser, bypass_rls = cursor.fetchone()
    if current not in allowed:
        raise PermissionDenied(
            "A role de banco desta máquina não pode abrir contexto cross-tenant."
        )
    if is_superuser or bypass_rls:
        raise PermissionDenied(
            "A role de sistema não pode ter SUPERUSER ou BYPASSRLS."
        )
    if current == settings.TENANT_SYSTEM_DB_ROLE:
        from .rls import ALL_TENANT_TABLES
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = current_schema()
                   AND c.relname = ANY(%s)
                   AND c.relowner = (
                       SELECT oid FROM pg_roles WHERE rolname = current_user
                   )
                """,
                [list(ALL_TENANT_TABLES)],
            )
            if cursor.fetchone()[0]:
                raise PermissionDenied(
                    "A role de workers não pode ser dona de tabela tenant."
                )


@contextmanager
def actor_context(actor):
    """Autoriza somente a descoberta das organizações do usuário autenticado."""
    actor_id = getattr(actor, "pk", actor)
    if not actor_id:
        raise ValueError("actor é obrigatório para resolver o tenant")
    token = _current_actor_id.set(str(actor_id))
    try:
        with transaction.atomic():
            _set_postgres_actor(actor_id, local=True)
            yield
    finally:
        try:
            _set_postgres_actor(None, local=False)
        except Exception:
            pass
        _current_actor_id.reset(token)


@contextmanager
def organization_context(organization):
    """Abre uma transação cujo RLS só enxerga uma organização."""
    organization_id = getattr(organization, "pk", organization)
    if not organization_id:
        raise ValueError("organization é obrigatória para contexto tenant")
    token_org = _current_organization_id.set(str(organization_id))
    token_system = _system_context.set(False)
    try:
        with transaction.atomic():
            _set_postgres_context(
                organization_id=organization_id, system=False, local=True,
            )
            yield
    finally:
        try:
            _set_postgres_context(local=False)
        except Exception:
            pass
        _system_context.reset(token_system)
        _current_organization_id.reset(token_org)


@contextmanager
def system_context():
    """Contexto explícito para catálogo global, migrações e suporte auditado."""
    if in_system_context():
        yield
        return
    token_org = _current_organization_id.set(None)
    token_system = _system_context.set(True)
    try:
        _assert_privileged_database_role()
        _set_postgres_context(system=True, local=False)
        yield
    finally:
        try:
            _set_postgres_context(system=False, local=False)
        except Exception:
            # A conexão pode ter caído; a próxima nasce sem contexto por default.
            pass
        _system_context.reset(token_system)
        _current_organization_id.reset(token_org)


def system_job(func):
    """Marca um management command inteiro como operação global explícita."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        with system_context():
            return func(*args, **kwargs)
    return wrapped


def organization_job(func):
    """Instala o tenant em threads/jobs cujo primeiro argumento é User ou user_id."""
    @wraps(func)
    def wrapped(user_or_id, *args, **kwargs):
        from django.contrib.auth import get_user_model
        from .models import organization_for_user

        close_old_connections()
        user = user_or_id
        if not getattr(user, "is_authenticated", False):
            user = get_user_model().objects.get(pk=user_or_id)
        try:
            with actor_context(user.pk):
                organization = organization_for_user(user)
                if organization is None:
                    raise ValueError("Job privado iniciado sem organização ativa.")
                with organization_context(organization):
                    return func(user_or_id, *args, **kwargs)
        finally:
            close_old_connections()
    return wrapped


def organization_callable(organization, func):
    """Adapta um callable sem argumentos para execução segura em nova thread."""
    organization_id = getattr(organization, "pk", organization)

    @wraps(func)
    def wrapped():
        close_old_connections()
        try:
            with organization_context(organization_id):
                return func()
        finally:
            close_old_connections()
    return wrapped


@receiver(connection_created)
def restore_context_on_reconnect(sender, connection, **kwargs):
    """Reinstala o escopo após reconnect de um worker longo."""
    if connection.vendor != "postgresql":
        return
    organization_id = current_organization_id()
    actor_id = current_actor_id()
    system = in_system_context()
    if not organization_id and not actor_id and not system:
        return
    organization_value = str(organization_id or "")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT set_config('app.organization_id', %s, false),
                   set_config('app.organization_signature', %s, false),
                   set_config('app.system_context', %s, false),
                   set_config('app.system_signature', %s, false)
            """,
            [
                organization_value,
                (
                    _context_signature("organization", organization_value)
                    if organization_value and not system
                    else ""
                ),
                "on" if system else "off",
                _context_signature("system") if system else "",
            ],
        )
        cursor.execute(
            """
            SELECT set_config('app.actor_id', %s, false),
                   set_config('app.actor_signature', %s, false)
            """,
            [
                str(actor_id or ""),
                (
                    _context_signature("actor", str(actor_id))
                    if actor_id
                    else ""
                ),
            ],
        )
