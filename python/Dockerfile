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

# Código da aplicação (django/) + os Procfiles de cada process group.
#
# O `Procfile` da raiz saiu: `[processes]` do fly.toml sempre sobrescreveu o CMD,
# então ele nunca foi lido em produção — era uma terceira cópia da lista de
# processos, livre para divergir das outras duas, e já tinha divergido.
COPY django/ ./django/
COPY Procfile.web Procfile.worker ./

# Estáticos compilados na build (servidos pelo WhiteNoise em runtime).
# DEBUG=0 aqui é OBRIGATÓRIO: gera o staticfiles.json (ManifestStaticFilesStorage)
# que o runtime exige. Secret só-de-build (o de verdade vem via Fly secret no runtime).
RUN DJANGO_DEBUG=0 DJANGO_SECRET_KEY=build-time-only-not-a-real-secret-000 \
    python django/manage.py collectstatic --noinput

EXPOSE 8080

# Em produção quem manda é `[processes]` do fly.toml (dois grupos, duas máquinas).
# Este CMD é só o padrão para quem roda a imagem à mão: sobe o grupo `web`, que é
# o que serve HTTP. Antes ele apontava para um Procfile que não existe mais.
CMD ["honcho", "-f", "Procfile.web", "start"]
