"""
Django settings for the ride-hailing backend.

All environment-specific values are read from environment variables (optionally
loaded from a `.env` file). Nothing secret is hard-coded here.
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, []),
    CORS_ALLOWED_ORIGINS=(list, []),
    CSRF_TRUSTED_ORIGINS=(list, []),
)

# A `.env` at the repo root is shared with docker compose; a backend-local one wins.
for env_file in (BASE_DIR.parent / ".env", BASE_DIR / ".env"):
    if env_file.exists():
        environ.Env.read_env(env_file, overwrite=False)

DEBUG = env("DJANGO_DEBUG")
SECRET_KEY = env("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt",
    "drf_spectacular",
    # Local
    "apps.common",
    "apps.accounts",
    "apps.drivers",
    "apps.locations",
    "apps.pricing",
    "apps.trips",
    "apps.payments",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

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
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- Database ---------------------------------------------------------------
DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["ATOMIC_REQUESTS"] = False  # explicit transactions in services
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DATABASE_CONN_MAX_AGE", default=60)
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Auth -------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- I18N / time ------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- Cache (used by throttling). Redis replaces LocMem in a later step. -----
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "ride-hailing-default",
    }
}

# --- Django REST Framework --------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_PAGINATION_CLASS": "apps.common.pagination.StandardPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.common.exceptions.api_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("THROTTLE_ANON", default="60/min"),
        "user": env("THROTTLE_USER", default="600/min"),
        "auth": env("THROTTLE_AUTH", default="10/min"),
        "location_update": env("THROTTLE_LOCATION_UPDATE", default="120/min"),
        "trip_create": env("THROTTLE_TRIP_CREATE", default="10/min"),
    },
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

if DEBUG:
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"].append(
        "rest_framework.renderers.BrowsableAPIRenderer"
    )

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_MINUTES", default=15)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_DAYS", default=7)),
    "SIGNING_KEY": env("JWT_SIGNING_KEY", default=SECRET_KEY),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "UPDATE_LAST_LOGIN": True,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Ride-Hailing API",
    "DESCRIPTION": "Rider/driver ride-hailing platform API (v1).",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {"RideCategoryEnum": "apps.drivers.models.RideCategory"},
}

# --- CORS / CSRF (explicit allow-list, never wildcard) ----------------------
CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = False  # JWT in Authorization header, not cookies
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")

# --- Domain settings --------------------------------------------------------
NEARBY_DRIVERS_DEFAULT_RADIUS_KM = env.float("NEARBY_DRIVERS_DEFAULT_RADIUS_KM", default=5.0)
NEARBY_DRIVERS_MAX_RADIUS_KM = env.float("NEARBY_DRIVERS_MAX_RADIUS_KM", default=20.0)
NEARBY_DRIVERS_MAX_RESULTS = env.int("NEARBY_DRIVERS_MAX_RESULTS", default=25)
FARE_CURRENCY = env("FARE_CURRENCY", default="USD")

# --- Location service (see docs/location-service.md) ------------------------
# Storage backend for latest driver locations (swap for a Redis store later).
LOCATION_STORE_BACKEND = env(
    "LOCATION_STORE_BACKEND", default="apps.locations.stores.PostgresLocationStore"
)
# A location older than this is "stale": the driver is not considered available.
DRIVER_LOCATION_STALE_SECONDS = env.int("DRIVER_LOCATION_STALE_SECONDS", default=120)
# Online drivers silent for longer than this are switched offline by
# `manage.py expire_stale_drivers` (run periodically).
DRIVER_AUTO_OFFLINE_SECONDS = env.int("DRIVER_AUTO_OFFLINE_SECONDS", default=900)
# Updates whose device timestamp is older than this are not applied as "latest".
LOCATION_MAX_AGE_SECONDS = env.int("LOCATION_MAX_AGE_SECONDS", default=300)
# Tolerated device clock drift into the future; beyond this the update is rejected.
LOCATION_MAX_FUTURE_SKEW_SECONDS = env.int("LOCATION_MAX_FUTURE_SKEW_SECONDS", default=30)
# Fixes reported with worse accuracy than this are ignored for matching.
LOCATION_MAX_ACCURACY_M = env.float("LOCATION_MAX_ACCURACY_M", default=500.0)
# Optional, sampled history (never one row per GPS ping).
LOCATION_HISTORY_ENABLED = env.bool("LOCATION_HISTORY_ENABLED", default=True)
LOCATION_HISTORY_MIN_INTERVAL_SECONDS = env.int("LOCATION_HISTORY_MIN_INTERVAL_SECONDS", default=30)
LOCATION_HISTORY_MIN_DISTANCE_M = env.float("LOCATION_HISTORY_MIN_DISTANCE_M", default=100.0)
# Record at least one heartbeat event this often even when stationary.
LOCATION_HISTORY_MAX_INTERVAL_SECONDS = env.int(
    "LOCATION_HISTORY_MAX_INTERVAL_SECONDS", default=300
)
LOCATION_HISTORY_RETENTION_DAYS = env.int("LOCATION_HISTORY_RETENTION_DAYS", default=30)

# --- H3 geographic indexing (see docs/h3-geo-indexing.md) -------------------
# Resolution of the cell stored with each latest location. 8 ~ 0.53 km edge.
# After changing it run `manage.py reindex_driver_locations` (rows also self-heal
# on the next GPS update).
H3_RESOLUTION = env.int("H3_RESOLUTION", default=8)
# Hard cap on ring expansion; radii needing more rings use the bbox fallback.
H3_MAX_GRID_DISTANCE = env.int("H3_MAX_GRID_DISTANCE", default=40)
# "h3" (default) or "bbox" (step 1/2 implementation, kept as fallback).
NEARBY_SEARCH_STRATEGY = env("NEARBY_SEARCH_STRATEGY", default="h3")

# --- Observability ----------------------------------------------------------
METRICS_BACKEND = env("METRICS_BACKEND", default="apps.common.metrics.LoggingMetrics")

# --- Security hardening for non-debug deployments ---------------------------
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=0)
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"

# --- Logging (with redaction of credentials / tokens) -----------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redact": {"()": "apps.common.logging.RedactSensitiveDataFilter"},
    },
    "formatters": {
        "text": {
            "()": "apps.common.logging.KeyValueFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
        "json": {"()": "apps.common.logging.JsonFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": env("LOG_FORMAT", default="json"),
            "filters": ["redact"],
        },
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
