# Celery é OPCIONAL: em produção a automação roda pelos loops do honcho
# (manage.py automacao), sem broker. Se o pacote celery estiver instalado
# expomos o app (worker/beat opt-in); senão o Django sobe normalmente.
try:
    from .celery import app as celery_app
    __all__ = ("celery_app",)
except ModuleNotFoundError:
    celery_app = None
    __all__ = ()
