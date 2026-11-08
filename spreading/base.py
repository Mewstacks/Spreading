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

# Atrás de TLS (Caddy, Fly): cookies só por HTTPS, redirect e HSTS. Local em http deixa desligado.
# O proxy tem de sobrescrever X-Forwarded-Proto; HSTS de 30 dias: subir só depois de ver tudo certo.
HTTPS = env_bool("DJANGO_HTTPS", False)
if HTTPS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SECURE_REDIRECT_EXEMPT = [r"^healthz$"]  # a sonda de saúde interna fala http
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(env("DJANGO_HSTS_SECONDS", "2592000"))

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
    "django.middleware.clickjacking.XFrameOptionsMiddleware",  # o painel tem botão que envia
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
# Dashboard do WAHA como o navegador do dono o enxerga (o passkey do WhatsApp passa por ele).
WAHA_DASHBOARD_URL = env("WAHA_DASHBOARD_URL", "http://localhost:3000/dashboard/")
HTTP_TIMEOUT_SECONDS = float(env("HTTP_TIMEOUT_SECONDS", "15"))
# Canário (comando `canary`): mensagem real a um chat privado e conferência do ACK.
# CANARY_CHAT_ID vale para o número principal; os demais: CANARY_CHATS=numero2=chat@g.us,…
CANARY_CHAT_ID = env("CANARY_CHAT_ID", "")
CANARY_CHATS = env("CANARY_CHATS", "")
# Teto de mensagens por hora em cada número de WhatsApp (soma de todos os grupos dele). 0 desliga.
SESSION_HOURLY_CAP = int(env("SESSION_HOURLY_CAP", "20"))

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
# Link curto com sub-id (Spreading + grupo) a cada envio da Shopee: a tela Receita separa o que o
# Spreading vendeu. Falhou, sai o link comum. 0 desliga.
SHOPEE_TRACK = env_bool("SHOPEE_TRACK", True)

# Amazon BR: coleta pela busca pública (sem API). 0 desliga.
AMAZON_COLLECT = env_bool("AMAZON_COLLECT", True)

# Vitrine pública de cupons do ML (cupons de campanha e ofertas em destaque). 0 desliga.
ML_PUBLIC_PAGES = env_bool("ML_PUBLIC_PAGES", True)

# Alertas (opt-in): WhatsApp para ALERT_CHAT_ID (por um número conectado) e/ou e-mail.
ALERT_CHAT_ID = env("ALERT_CHAT_ID", "")
ALERT_EMAIL_TO = env("ALERT_EMAIL_TO", "")
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "spreading@localhost")
