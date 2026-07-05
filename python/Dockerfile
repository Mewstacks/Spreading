# Imagem do app Django (web + workers de automação + Playwright).
# Contexto de build = pasta python/. App em django/, deps em requirements.txt.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=core.settings \
    # Chromium do Playwright fica na imagem (não no volume).
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

WORKDIR /app

# Deps de sistema p/ Chromium headless + psycopg + build de wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

# Código da aplicação (django/) + Procfile dos processos.
COPY django/ ./django/
COPY Procfile ./

# Estáticos compilados na build (servidos pelo WhiteNoise em runtime).
# DEBUG=0 aqui é OBRIGATÓRIO: gera o staticfiles.json (ManifestStaticFilesStorage)
# que o runtime exige. Secret só-de-build (o de verdade vem via Fly secret no runtime).
RUN DJANGO_DEBUG=0 DJANGO_SECRET_KEY=build-time-only-not-a-real-secret-000 \
    python django/manage.py collectstatic --noinput

EXPOSE 8080

# honcho sobe todos os processos do Procfile numa única máquina (o volume de
# sessões do ML fica preso a UMA máquina no Fly — web e workers compartilham).
CMD ["honcho", "start"]
