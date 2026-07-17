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
from django.db import connection
from django.http import HttpResponse
from django.urls import path, include
from apps.scrapers import views as scraper_views


@login_not_required
def healthz(request):
    """Readiness público: processo vivo e banco realmente utilizável."""
    # Normalmente o DatabaseUnavailableMiddleware intercepta esta rota antes da
    # sessão. Mantemos a mesma semântica caso a view seja chamada isoladamente.
    try:
        connection.close()
        with connection.cursor() as cursor:
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
    path('admin/', admin.site.urls),
    path('accounts/', include('apps.accounts.urls')),
    path('scrapers/', include('apps.scrapers.urls')),
]
