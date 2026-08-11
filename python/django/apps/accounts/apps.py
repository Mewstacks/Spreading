from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    verbose_name = "Contas"

    def ready(self):
        # Registra signals e checks de segurança no carregamento do app.
        from . import checks  # noqa: F401
        from . import models  # noqa: F401
        # Os receivers que invalidam o contexto de organização cacheado moram no
        # middleware. Importar aqui garante que um processo que NÃO monta a cadeia
        # de middleware (workers, management commands) também derrube a entrada ao
        # mexer num vínculo — com Redis o cache é compartilhado entre todos eles.
        from . import organization_middleware  # noqa: F401
