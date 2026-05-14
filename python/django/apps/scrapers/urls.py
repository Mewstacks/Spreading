from django.urls import path
from apps.scrapers import views

urlpatterns = [
    path("", views.dashboard, name="scraper-dashboard"),
    path("run/", views.run_scraper_stream, name="scraper-run"),
    path("auth/", views.auth_stream, name="scraper-auth"),
    path("top/", views.top_promocoes, name="scraper-top"),
]
