import hmac

from django.conf import settings
from django.contrib import admin
from django.http import HttpResponseForbidden, JsonResponse
from django.urls import path

from apps.delivery.health import snapshot
from apps.delivery.transports import WahaTransport
from apps.delivery.views import pair, pair_qr


def healthz(_request):
    return JsonResponse({"status": "ok"})


def status(request):
    """Saúde detalhada; exige o token (nada de dado operacional público)."""
    token = settings.STATUS_TOKEN
    given = request.headers.get("X-Status-Token", "")
    if not token or not hmac.compare_digest(given, token):
        return HttpResponseForbidden("forbidden")
    return JsonResponse(snapshot(WahaTransport()))


urlpatterns = [
    path("healthz", healthz),
    path("status", status),
    path("pair", pair),
    path("pair/qr.png", pair_qr),
    path("admin/", admin.site.urls),
]
