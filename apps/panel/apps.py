from django.apps import AppConfig


class Config(AppConfig):
    name = "apps.panel"
    label = "panel"

    def ready(self):
        from django.contrib import admin

        admin.site.login_template = "panel/login.html"  # entrada no padrão visual do painel
