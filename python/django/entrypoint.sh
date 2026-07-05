#!/usr/bin/env sh
# Entrypoint do container web. Aplica migrações no start (idempotente) — funciona
# tanto com SQLite no volume /data quanto com Postgres (DATABASE_URL). Depois
# entrega o controle pro comando do process group (gunicorn / celery).
set -e

# Só o process group 'web' migra, pra evitar corrida se worker/beat subirem juntos.
if [ "${FLY_PROCESS_GROUP:-web}" = "web" ]; then
  echo "→ Aplicando migrações..."
  python manage.py migrate --noinput
fi

exec "$@"
