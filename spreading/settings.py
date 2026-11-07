import os
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return str(env(name, str(default))).lower() in {"1", "true", "yes", "on"}


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.orgs.apps.Config",
    "apps.coupons.apps.Config",
    "apps.delivery.apps.Config",
    "apps.compose.apps.Config",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "spreading.urls"
WSGI_APPLICATION = "spreading.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]


def _database():
    url = env("DATABASE_URL", "")
    if not url:
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
    parsed = urlparse(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": parsed.port or "",
        "CONN_MAX_AGE": 60,
    }


DATABASES = {"default": _database()}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Fontes e limites
ML_COUPON_FEED_URL = env(
    "ML_COUPON_FEED_URL",
    "https://afiliadosmercadolivre.github.io/cupons-afiliadosmercadolivre/",
)
FEED_MAX_AGE_SECONDS = int(env("FEED_MAX_AGE_SECONDS", "3600"))
WAHA_URL = env("WAHA_URL", "http://waha:3000")
WAHA_API_KEY = env("WAHA_API_KEY", "")
WAHA_SESSION = env("WAHA_SESSION", "default")
HTTP_TIMEOUT_SECONDS = float(env("HTTP_TIMEOUT_SECONDS", "15"))

if not DEBUG and SECRET_KEY.startswith("dev-only"):
    # Em produção a chave tem de vir do ambiente; em teste/dev local segue o padrão.
    if env("DJANGO_ENV", "dev") == "prod":
        raise RuntimeError("DJANGO_SECRET_KEY obrigatório em produção")
