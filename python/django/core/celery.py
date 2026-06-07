import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

app = Celery("core")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Agenda padrão. Ajuste a cadência conforme necessário (ou use o admin do
# django_celery_beat para editar sem deploy).
app.conf.beat_schedule = {
    # Raspagem + categorização + pré-geração de links 1x/dia (horário ajustável)
    "scrape-ml-diario": {
        "task": "apps.scrapers.tasks.task_scrape",
        "schedule": crontab(hour=3, minute=0),
    },
    # Tick de envios a cada 5 min; o intervalo real é por ConfiguracaoEnvio
    "tick-envios-5min": {
        "task": "apps.scrapers.tasks.task_tick_envios",
        "schedule": crontab(minute="*/5"),
    },
}
