# Django + Celery + Playwright (chromium) — imagem única usada pelos 3 process
# groups do Fly (web/worker/beat). O scraping e o Link Builder de afiliado rodam
# Playwright localmente, então o chromium precisa estar na imagem.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Dependências Python primeiro (camada cacheável).
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Chromium + libs de sistema do Playwright (versão casa com a do requirements).
RUN playwright install --with-deps chromium

# Código da aplicação.
COPY django/ ./django/
WORKDIR /app/django

# Static do admin/etc empacotado na imagem (WhiteNoise serve em runtime).
# SECRET_KEY dummy só para o collectstatic — não vai para runtime.
RUN DJANGO_DEBUG=1 DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput

RUN chmod +x entrypoint.sh

EXPOSE 8080

# migrate no start; depois o comando abaixo (web). worker/beat sobrescrevem no fly.toml.
ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "core.wsgi:application", "--bind", "0.0.0.0:8080", \
     "--workers", "2", "--threads", "4", "--timeout", "120", "--access-logfile", "-"]
