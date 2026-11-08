from django.apps import AppConfig


class Config(AppConfig):
    name = "apps.orgs"
    label = "orgs"

    def ready(self):
        from . import throttle

        throttle.connect()
