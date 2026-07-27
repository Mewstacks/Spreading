from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    verbose_name = "Contas"

    def ready(self):
        # Registra signals e checks de segurança no carregamento do app.
        from . import checks  # noqa: F401
        from . import models  # noqa: F401
