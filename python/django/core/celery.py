import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

app = Celery("core")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Produção usa exclusivamente os loops explícitos do Procfile. As tasks seguem
# disponíveis para execução manual, sem um segundo agendador concorrente.
app.conf.beat_schedule = {}
