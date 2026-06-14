from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    verbose_name = "Contas"

    def ready(self):
        # Registra o signal post_save que cria o Perfil de cada User.
        from . import models  # noqa: F401
