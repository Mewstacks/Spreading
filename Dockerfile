FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Chave descartável só para o collectstatic; a real vem do ambiente em runtime.
RUN DJANGO_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(64))')" \
    python manage.py collectstatic --noinput
CMD ["gunicorn", "spreading.wsgi", "--bind", "0.0.0.0:8000", "--workers", "2"]
