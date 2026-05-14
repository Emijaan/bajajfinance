"""Django settings for the bajaj_soa project."""

from __future__ import annotations

from pathlib import Path

from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config(
    "DJANGO_SECRET_KEY",
    default="dev-insecure-secret-do-not-use-in-prod-please-change-me",
)
DEBUG = config("DJANGO_DEBUG", default=False, cast=bool)

_hosts = config(
    "DJANGO_ALLOWED_HOSTS",
    default="*",
    cast=Csv(),
)
# Any entry `*` means accept any Host header (LAN, tunnels, etc.).
ALLOWED_HOSTS = (
    ["*"]
    if (not _hosts or any((h or "").strip() == "*" for h in _hosts))
    else list(_hosts)
)
CSRF_TRUSTED_ORIGINS = config(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    default="http://localhost:8000,http://127.0.0.1:8000,http://192.168.0.5:8000",
    cast=Csv(),
)

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "soa",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "bajaj_soa.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "bajaj_soa.wsgi.application"
ASGI_APPLICATION = "bajaj_soa.asgi.application"

# SQLite for batch job metadata + file paths (async payment reports).
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "app.sqlite3",
        "OPTIONS": {
            "timeout": 30,
        },
    }
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Small cookie-backed session (batch job UUIDs); avoids extra DB tables for sessions.
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Tighten cookies when running over HTTPS in production.
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {
            "format": "{asctime} {levelname:<7} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "soa": {
            "handlers": ["console"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

# --- Bajaj DMS credentials (consumed by soa.client.BajajClient) ---
BAJAJ_USERNAME = config("BAJAJ_USERNAME", default="")
BAJAJ_PASSWORD = config("BAJAJ_PASSWORD", default="")

BAJAJ_SESSION_RENEW_BUFFER_SECONDS = config(
    "BAJAJ_SESSION_RENEW_BUFFER_SECONDS",
    default=180,
    cast=float,
)
BAJAJ_SOA_FETCH_MAX_RETRIES = config(
    "BAJAJ_SOA_FETCH_MAX_RETRIES",
    default=8,
    cast=int,
)

# --- Batch payment report (soa.batch_report / payment_report view) ---
SOA_BATCH_DELAY_SECONDS = config("SOA_BATCH_DELAY_SECONDS", default=0.35, cast=float)
SOA_BATCH_MAX_LOANS = config("SOA_BATCH_MAX_LOANS", default=20000, cast=int)
SOA_BATCH_FETCH_WORKERS = config(
    "SOA_BATCH_FETCH_WORKERS",
    default=10,
    cast=int,
)
SOA_BATCH_MAX_UPLOAD_BYTES = config(
    "SOA_BATCH_MAX_UPLOAD_BYTES", default=10_485_760, cast=int
)

# Delete batch job files older than this many days (see management command).
SOA_BATCH_JOB_RETENTION_DAYS = config("SOA_BATCH_JOB_RETENTION_DAYS", default=7, cast=int)
