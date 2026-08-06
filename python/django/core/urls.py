"""
URL configuration for core project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.contrib.auth.decorators import login_not_required
from django.conf import settings
from django.db import connection
from django.http import HttpResponse
from django.urls import path, include
from apps.scrapers import hooks as scraper_hooks
from apps.scrapers import views as scraper_views


@login_not_required
def healthz(request):
    """Readiness público: processo vivo e banco realmente utilizável."""
    # Normalmente o DatabaseUnavailableMiddleware intercepta esta rota antes da
    # sessão. Mantemos a mesma semântica caso a view seja chamada isoladamente.
    try:
        connection.close()
        with connection.cursor() as cursor:
            if settings.APP_ENV in {"staging", "production"}:
                if connection.vendor != "postgresql":
                    raise RuntimeError("backend de produção não é PostgreSQL")
                cursor.execute(
                    """
                    SELECT current_user, r.rolsuper, r.rolbypassrls
                      FROM pg_roles r
                     WHERE r.rolname = current_user
                    """
                )
                role, is_superuser, bypass_rls = cursor.fetchone()
                if role != settings.TENANT_RUNTIME_DB_ROLE:
                    raise RuntimeError("role do processo web não é a role de runtime")
                if is_superuser or bypass_rls:
                    raise RuntimeError("role do processo web pode ignorar RLS")
                from apps.accounts.rls import ALL_TENANT_TABLES
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
                    raise RuntimeError("role do processo web é dona de tabela tenant")
                if settings.PHASE0_EXPAND_ONLY:
                    return HttpResponse(
                        "ok-expand-only",
                        content_type="text/plain",
                    )
                cursor.execute(
                    """
                    SELECT count(*)
                      FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = current_schema()
                       AND c.relname = ANY(%s)
                       AND c.relrowsecurity
                       AND c.relforcerowsecurity
                    """,
                    [list(ALL_TENANT_TABLES)],
                )
                if cursor.fetchone()[0] != len(ALL_TENANT_TABLES):
                    raise RuntimeError("RLS não está ENABLE + FORCE em todas as tabelas")
                cursor.execute(
                    """
                    SELECT count(*)
                      FROM pg_policies
                     WHERE schemaname = current_schema()
                       AND tablename = ANY(%s)
                       AND policyname = ANY(%s)
                       AND (
                           COALESCE(qual, '') || COALESCE(with_check, '')
                       ) LIKE '%%tenant_security.context_valid%%'
                    """,
                    [
                        list(ALL_TENANT_TABLES),
                        [
                            "tenant_select",
                            "tenant_insert",
                            "tenant_update",
                            "tenant_delete",
                        ],
                    ],
                )
                if cursor.fetchone()[0] != len(ALL_TENANT_TABLES) * 4:
                    raise RuntimeError("policies RLS assinadas estão incompletas")
                cursor.execute(
                    """
                    SELECT p.prosecdef,
                           owner.rolname,
                           has_function_privilege(
                               current_user, p.oid, 'EXECUTE'
                           ),
                           has_table_privilege(
                               current_user,
                               'tenant_security.context_secret',
                               'SELECT'
                           )
                      FROM pg_proc p
                      JOIN pg_namespace n ON n.oid = p.pronamespace
                      JOIN pg_roles owner ON owner.oid = p.proowner
                     WHERE n.nspname = 'tenant_security'
                       AND p.proname = 'context_valid'
                       AND p.pronargs = 3
                    """
                )
                signed_context = cursor.fetchone()
                if (
                    not signed_context
                    or not signed_context[0]
                    or signed_context[1] != settings.TENANT_MIGRATION_DB_ROLE
                    or not signed_context[2]
                    or signed_context[3]
                ):
                    raise RuntimeError("contexto tenant assinado está inseguro")
            else:
                cursor.execute("SELECT 1")
                cursor.fetchone()
    except Exception:
        connection.close()
        return HttpResponse("database unavailable", status=503, content_type="text/plain")
    finally:
        connection.close()
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path('', scraper_views.operations_dashboard, name='home'),
    # Link curto das mensagens (contagem de cliques). Fica na raiz de propósito:
    # cada caractere a menos conta dentro da mensagem do WhatsApp/Telegram.
    path('r/<str:slug>/', scraper_views.redirect_curto, name='redirect-curto'),
    path('healthz', healthz, name='healthz'),
    # Webhook do Sentry → repository_dispatch no GitHub (workflow de autofix).
    # Público por necessidade; autenticado por HMAC em apps.scrapers.hooks.
    path('hooks/sentry/', scraper_hooks.sentry_hook, name='sentry-hook'),
    path('admin/', admin.site.urls),
    path('accounts/', include('apps.accounts.urls')),
    path('scrapers/', include('apps.scrapers.urls')),
]
