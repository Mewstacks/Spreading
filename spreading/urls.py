from django.contrib import admin
from django.http import JsonResponse
from django.urls import path


def healthz(_request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("healthz", healthz),
    path("admin/", admin.site.urls),
]
