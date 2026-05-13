from django.urls import path
from apps.scrapers import views

urlpatterns = [
    path("", views.dashboard, name="scraper-dashboard"),
    path("run/", views.run_scraper_stream, name="scraper-run"),
]
