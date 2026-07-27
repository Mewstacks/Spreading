from django.apps import AppConfig


class ScrapersConfig(AppConfig):
    name = 'apps.scrapers'

    def ready(self):
        from . import tenant_signals  # noqa: F401
