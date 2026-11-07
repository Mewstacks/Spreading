import os
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return str(env(name, str(default))).lower() in {"1", "true", "yes", "on"}


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
    "apps.offers.apps.Config",
    "apps.linkbuilder.apps.Config",
    "apps.panel.apps.Config",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
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
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Fontes e limites
ML_COUPON_FEED_URL = env(
    "ML_COUPON_FEED_URL",
    "https://afiliadosmercadolivre.github.io/cupons-afiliadosmercadolivre/",
)
FEED_MAX_AGE_SECONDS = int(env("FEED_MAX_AGE_SECONDS", "3600"))
STATUS_TOKEN = env("STATUS_TOKEN", "")
ML_API_CLIENT_ID = env("ML_API_CLIENT_ID", "")
ML_API_CLIENT_SECRET = env("ML_API_CLIENT_SECRET", "")
WAHA_URL = env("WAHA_URL", "http://waha:3000")
WAHA_API_KEY = env("WAHA_API_KEY", "")
WAHA_SESSION = env("WAHA_SESSION", "default")
HTTP_TIMEOUT_SECONDS = float(env("HTTP_TIMEOUT_SECONDS", "15"))

# Link Builder do ML (gera o meli.la do dono). Desligado por padrão: precisa de sessão salva.
LINKBUILDER_ENABLED = env_bool("LINKBUILDER_ENABLED", False)
LINKBUILDER_STATE = env("LINKBUILDER_STATE", "/secrets/ml-state.json")
LINKBUILDER_MAX_PER_RUN = int(env("LINKBUILDER_MAX_PER_RUN", "8"))
LINKBUILDER_MAX_PER_DAY = int(env("LINKBUILDER_MAX_PER_DAY", "120"))
LINKBUILDER_PACE_SECONDS = float(env("LINKBUILDER_PACE_SECONDS", "4"))
LINKBUILDER_VERIFY = env_bool("LINKBUILDER_VERIFY", True)

# Shopee Affiliate Open API (App ID e Secret do painel de afiliados). Sem eles a loja não coleta.
SHOPEE_APP_ID = env("SHOPEE_APP_ID", "")
SHOPEE_APP_SECRET = env("SHOPEE_APP_SECRET", "")

# Amazon BR: coleta pela busca pública (sem API). 0 desliga.
AMAZON_COLLECT = env_bool("AMAZON_COLLECT", True)
