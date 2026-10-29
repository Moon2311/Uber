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

# development | test | staging | production. Staging and production refuse DEBUG
# and are held to the deployment checks in apps/common/checks.py.
APP_ENV = env("APP_ENV", default="development")
APP_RELEASE = env("APP_RELEASE", default="")  # image tag / git sha, reported in logs & errors
DEBUG = env("DJANGO_DEBUG")
SECRET_KEY = env("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")
IS_DEPLOYED = APP_ENV in ("staging", "production")
if IS_DEPLOYED and DEBUG:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(f"DJANGO_DEBUG must be false when APP_ENV={APP_ENV}")

INSTALLED_APPS = [
    "daphne",  # ASGI server: `runserver` then serves HTTP + WebSockets (must be first)
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "channels",
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",  # refresh-token rotation + logout
    "drf_spectacular",
    # Local
    "apps.common",
    "apps.accounts",
    "apps.drivers",
    "apps.locations",
    "apps.pricing",
    "apps.trips",
    "apps.payments",
    "apps.events",
    "apps.analytics",
    "apps.matching",
    "apps.realtime",
    "apps.fraud",
    "apps.notifications",
]

MIDDLEWARE = [
    "apps.common.middleware.RequestContextMiddleware",  # first: covers everything below
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
ASGI_APPLICATION = "config.asgi.application"

# --- Database ---------------------------------------------------------------
DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["ATOMIC_REQUESTS"] = False  # explicit transactions in services
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DATABASE_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True  # drop dead pooled connections
# Bounded waits: a stuck lock or runaway query fails fast (-> 503, retryable) instead
# of pinning a worker. Migrations / backfills run with their own (0 = no limit).
_pg_options = " ".join(
    f"-c {name}={env.int(var, default=default)}"
    for name, var, default in [
        ("statement_timeout", "DATABASE_STATEMENT_TIMEOUT_MS", 15000),
        ("lock_timeout", "DATABASE_LOCK_TIMEOUT_MS", 5000),
        ("idle_in_transaction_session_timeout", "DATABASE_IDLE_TX_TIMEOUT_MS", 60000),
    ]
)
DATABASES["default"].setdefault("OPTIONS", {})
DATABASES["default"]["OPTIONS"].update(
    connect_timeout=env.int("DATABASE_CONNECT_TIMEOUT_S", default=5), options=_pg_options
)
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
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if IS_DEPLOYED
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}
if IS_DEPLOYED:  # admin static files (collected into the image), hashed + cached
    MIDDLEWARE.insert(MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,
                      "whitenoise.middleware.WhiteNoiseMiddleware")  # fmt: skip
# The API takes small JSON bodies only; refuse large ones early.
DATA_UPLOAD_MAX_MEMORY_SIZE = env.int("DATA_UPLOAD_MAX_MEMORY_SIZE", default=256 * 1024)
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200

# --- Cache (rate-limit counters) ---------------------------------------------
# Throttling MUST share state across processes/replicas, so deployed environments
# use Redis. The resilient backend fails OPEN (no throttling, metric + log) if Redis
# is down, instead of failing every request.
CACHE_BACKEND = env("CACHE_BACKEND", default="redis" if IS_DEPLOYED else "locmem")
if CACHE_BACKEND == "redis":
    CACHES = {
        "default": {
            "BACKEND": "apps.common.cache.ResilientRedisCache",
            "LOCATION": env(
                "CACHE_URL", default=env("REDIS_URL", default="redis://localhost:6379/1")
            ),
            "KEY_PREFIX": "rh-cache",
            "OPTIONS": {"socket_timeout": 0.25, "socket_connect_timeout": 0.25},
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "ride-hailing-default",
        }
    }

# --- Django REST Framework --------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.JWTAuthenticationWithProfile",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_PAGINATION_CLASS": "apps.common.pagination.StandardPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.common.exceptions.api_exception_handler",
    # Client IP for throttling = the address the Nth proxy from the right saw.
    # Behind one load balancer: 1. Wrong value = everyone shares one bucket.
    "NUM_PROXIES": env.int("NUM_PROXIES", default=None),
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
        "fare_estimate": env("THROTTLE_FARE_ESTIMATE", default="30/min"),
        # Per target account (not per IP): stops distributed password guessing.
        "auth_email": env("THROTTLE_AUTH_EMAIL", default="20/hour"),
        "webhook": env("THROTTLE_WEBHOOK", default="600/min"),
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
    # Each refresh returns a new refresh token and blacklists the old one, so a
    # stolen refresh token dies at its next legitimate use, and logout is real.
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}
# Interactive API docs: on in development, off in deployed environments by default.
API_DOCS_ENABLED = env.bool("API_DOCS_ENABLED", default=not IS_DEPLOYED)
# Moving the admin off /admin/ cuts automated scanning; it still requires staff login.
ADMIN_URL_PATH = env("ADMIN_URL_PATH", default="admin/")

SPECTACULAR_SETTINGS = {
    "TITLE": "Ride-Hailing API",
    "DESCRIPTION": "Rider/driver ride-hailing platform API (v1).",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "RideCategoryEnum": "apps.drivers.models.RideCategory",
        "ActorTypeEnum": "apps.trips.models.ActorType",
        "TripStatusEnum": "apps.trips.models.TripStatus",
        "PaymentStatusEnum": "apps.payments.models.PaymentStatus",
        "NotificationStatusEnum": "apps.notifications.models.NotificationStatus",
    },
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
# Hot store for live driver locations: "redis" (default) or "postgres".
LOCATION_STORE = env("LOCATION_STORE", default="redis")
# When Redis is unavailable: "postgres" (degrade to PostgreSQL) or "none" (503).
LOCATION_STORE_FALLBACK = env("LOCATION_STORE_FALLBACK", default="postgres")
# After a Redis failure, skip Redis for this long before trying again.
LOCATION_STORE_CIRCUIT_COOLDOWN_SECONDS = env.float(
    "LOCATION_STORE_CIRCUIT_COOLDOWN_SECONDS", default=5.0
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

# --- Redis ------------------------------------------------------------------
REDIS_URL = env("REDIS_URL", default="redis://localhost:6380/0")
# All keys live under "<prefix>:{live}:" (the hash tag keeps them in one cluster slot
# so Lua scripts may touch several keys).
REDIS_KEY_PREFIX = env("REDIS_KEY_PREFIX", default="rh")
REDIS_SOCKET_TIMEOUT = env.float("REDIS_SOCKET_TIMEOUT", default=0.5)
REDIS_CONNECT_TIMEOUT = env.float("REDIS_CONNECT_TIMEOUT", default=0.5)
REDIS_RETRY_ATTEMPTS = env.int("REDIS_RETRY_ATTEMPTS", default=2)
REDIS_HEALTH_CHECK_INTERVAL = env.int("REDIS_HEALTH_CHECK_INTERVAL", default=30)
# Live driver keys expire this long after the last write (status + location).
# Defaults to the auto-offline window.
LIVE_DRIVER_TTL_SECONDS = env.int("LIVE_DRIVER_TTL_SECONDS", default=DRIVER_AUTO_OFFLINE_SECONDS)

# --- Kafka / events (see docs/event-streaming.md) ----------------------------
KAFKA_ENABLED = env.bool("KAFKA_ENABLED", default=True)
KAFKA_BOOTSTRAP_SERVERS = env("KAFKA_BOOTSTRAP_SERVERS", default="localhost:9094")
KAFKA_CLIENT_ID = env("KAFKA_CLIENT_ID", default="ride-hailing")
# Prefix for topic and consumer-group names (isolates environments / test runs).
KAFKA_TOPIC_PREFIX = env("KAFKA_TOPIC_PREFIX", default="")
KAFKA_REPLICATION_FACTOR = env.int("KAFKA_REPLICATION_FACTOR", default=1)
KAFKA_DELIVERY_TIMEOUT_SECONDS = env.float("KAFKA_DELIVERY_TIMEOUT_SECONDS", default=10.0)
# Outbox relay
OUTBOX_BATCH_SIZE = env.int("OUTBOX_BATCH_SIZE", default=200)
OUTBOX_POLL_INTERVAL_SECONDS = env.float("OUTBOX_POLL_INTERVAL_SECONDS", default=0.5)
OUTBOX_RETRY_BASE_SECONDS = env.float("OUTBOX_RETRY_BASE_SECONDS", default=1.0)
OUTBOX_RETRY_MAX_SECONDS = env.float("OUTBOX_RETRY_MAX_SECONDS", default=60.0)
# ~50 attempts with backoff capped at 60 s = ~45 min of retrying before giving up.
OUTBOX_MAX_ATTEMPTS = env.int("OUTBOX_MAX_ATTEMPTS", default=50)
OUTBOX_RETENTION_DAYS = env.int("OUTBOX_RETENTION_DAYS", default=7)
# Consumers
CONSUMER_MAX_RETRIES = env.int("CONSUMER_MAX_RETRIES", default=4)
CONSUMER_RETRY_BASE_SECONDS = env.float("CONSUMER_RETRY_BASE_SECONDS", default=0.5)
CONSUMER_RETRY_MAX_SECONDS = env.float("CONSUMER_RETRY_MAX_SECONDS", default=8.0)
CONSUMER_INFRA_PAUSE_SECONDS = env.float("CONSUMER_INFRA_PAUSE_SECONDS", default=5.0)
CONSUMER_HEARTBEAT_SECONDS = env.float("CONSUMER_HEARTBEAT_SECONDS", default=10.0)
PROCESSED_EVENT_RETENTION_DAYS = env.int("PROCESSED_EVENT_RETENTION_DAYS", default=14)
# Health thresholds
EVENTS_OUTBOX_MAX_AGE_SECONDS = env.int("EVENTS_OUTBOX_MAX_AGE_SECONDS", default=60)
EVENTS_CONSUMER_MAX_LAG = env.int("EVENTS_CONSUMER_MAX_LAG", default=1000)
EVENTS_CONSUMER_STALE_SECONDS = env.int("EVENTS_CONSUMER_STALE_SECONDS", default=60)
# Sampled location stream
LOCATION_EVENTS_ENABLED = env.bool("LOCATION_EVENTS_ENABLED", default=True)
LOCATION_EVENT_MIN_INTERVAL_SECONDS = env.int("LOCATION_EVENT_MIN_INTERVAL_SECONDS", default=15)

# --- Matching (see docs/matching-engine.md) ----------------------------------
MATCHING_OFFER_TTL_SECONDS = env.int("MATCHING_OFFER_TTL_SECONDS", default=15)
# Redis reservation lock outlives the offer slightly (released explicitly anyway).
MATCHING_LOCK_GRACE_SECONDS = env.int("MATCHING_LOCK_GRACE_SECONDS", default=5)
MATCHING_TIMEOUT_SECONDS = env.int("MATCHING_TIMEOUT_SECONDS", default=120)
MATCHING_MAX_OFFERS_PER_TRIP = env.int("MATCHING_MAX_OFFERS_PER_TRIP", default=5)
MATCHING_MAX_RESERVE_ATTEMPTS = env.int("MATCHING_MAX_RESERVE_ATTEMPTS", default=3)
MATCHING_TARGET_CANDIDATES = env.int("MATCHING_TARGET_CANDIDATES", default=5)
MATCHING_MAX_K = env.int("MATCHING_MAX_K", default=12)
MATCHING_MAX_RADIUS_KM = env.float("MATCHING_MAX_RADIUS_KM", default=6.0)
MATCHING_ETA_SHORTLIST = env.int("MATCHING_ETA_SHORTLIST", default=8)
MATCHING_RETRY_BASE_SECONDS = env.float("MATCHING_RETRY_BASE_SECONDS", default=2.0)
MATCHING_RETRY_MAX_SECONDS = env.float("MATCHING_RETRY_MAX_SECONDS", default=15.0)
MATCHING_LEASE_SECONDS = env.int("MATCHING_LEASE_SECONDS", default=30)
MATCHING_WORKER_POLL_SECONDS = env.float("MATCHING_WORKER_POLL_SECONDS", default=0.5)
MATCHING_WORKER_BATCH = env.int("MATCHING_WORKER_BATCH", default=20)
MATCHING_MIN_DRIVER_RATING = env.float("MATCHING_MIN_DRIVER_RATING", default=0.0)
# "redis" (advisory fast-path lock) or "none" (PostgreSQL only).
MATCHING_RESERVATION_LOCKS = env("MATCHING_RESERVATION_LOCKS", default="redis")
# ride category -> vehicle categories allowed to serve it (strict by default).
MATCHING_CATEGORY_COMPATIBILITY = {
    "ECONOMY": ["ECONOMY"],
    "COMFORT": ["COMFORT"],
    "XL": ["XL"],
}
MATCHING_RANKING = {
    "eta_weight": 1.0,
    "rating_weight_seconds": 60.0,
    "fallback_eta_penalty_seconds": 0.0,
    "max_pickup_eta_seconds": 900,
}
# Pickup ETA: "fallback" (labelled straight-line estimate) or "osrm".
ETA_PROVIDER = env("ETA_PROVIDER", default="fallback")
ETA_OSRM_URL = env("ETA_OSRM_URL", default="")
ETA_TIMEOUT_SECONDS = env.float("ETA_TIMEOUT_SECONDS", default=1.5)
ETA_FALLBACK_ROAD_FACTOR = 1.4
ETA_FALLBACK_SPEED_KMH = 22.0
ETA_FALLBACK_OVERHEAD_S = 60

# --- Pricing (Step 9) ---------------------------------------------------------
# Rules are versioned rows (manage.py pricing_rules); these bound every version.
# SURGE IS AN ILLUSTRATIVE POLICY: check local law (price caps, emergency pricing,
# disclosure) before enabling it for real riders. See docs/pricing-payments.md.
PRICING_QUOTE_TTL_SECONDS = env.int("PRICING_QUOTE_TTL_SECONDS", default=300)
PRICING_SURGE_ENABLED = env.bool("PRICING_SURGE_ENABLED", default=True)  # global kill switch
PRICING_SURGE_HARD_CAP = env.float("PRICING_SURGE_HARD_CAP", default=3.0)
PRICING_SURGE_DISABLED_ZONES = env.list("PRICING_SURGE_DISABLED_ZONES", default=[])
PRICING_SURGE_H3_RESOLUTION = env.int("PRICING_SURGE_H3_RESOLUTION", default=7)  # ~5 km2 zones
PRICING_SURGE_INTERVAL_SECONDS = env.float("PRICING_SURGE_INTERVAL_SECONDS", default=30)

# --- Payments -------------------------------------------------------------------
PAYMENTS_PROVIDER = env("PAYMENTS_PROVIDER", default="sandbox")
PAYMENTS_SANDBOX_ALLOWED = env.bool("PAYMENTS_SANDBOX_ALLOWED", default=DEBUG)
# Comma-separated; the first signs (sandbox), all verify (rotation). Never logged.
PAYMENTS_WEBHOOK_SECRETS = env.list("PAYMENTS_WEBHOOK_SECRETS", default=[])
PAYMENTS_WEBHOOK_TOLERANCE_SECONDS = env.int("PAYMENTS_WEBHOOK_TOLERANCE_SECONDS", default=300)
PAYMENTS_MAX_CHARGE_ATTEMPTS = env.int("PAYMENTS_MAX_CHARGE_ATTEMPTS", default=5)
PAYMENTS_SUBMITTED_GRACE_SECONDS = env.int("PAYMENTS_SUBMITTED_GRACE_SECONDS", default=60)
PAYMENTS_PENDING_CHECK_SECONDS = env.int("PAYMENTS_PENDING_CHECK_SECONDS", default=300)
PAYMENTS_UNAVAILABLE_RETRY_SECONDS = env.int("PAYMENTS_UNAVAILABLE_RETRY_SECONDS", default=60)
PAYMENTS_RECONCILE_BASE_SECONDS = env.float("PAYMENTS_RECONCILE_BASE_SECONDS", default=30)
PAYMENTS_RECONCILE_MAX_SECONDS = env.float("PAYMENTS_RECONCILE_MAX_SECONDS", default=1800)
PAYMENTS_MAX_RECONCILE_CHECKS = env.int("PAYMENTS_MAX_RECONCILE_CHECKS", default=12)
PAYMENTS_WORKER_BATCH = env.int("PAYMENTS_WORKER_BATCH", default=20)
PAYMENTS_WORKER_POLL_SECONDS = env.float("PAYMENTS_WORKER_POLL_SECONDS", default=1.0)

# --- Blocking pre-action checks (apps.common.guards) ----------------------------------
ACTION_GUARDS = {
    "trip_request": ["apps.payments.guards.no_outstanding_balance", "apps.fraud.guards.check"],
    "add_payment_method": ["apps.fraud.guards.check"],
}

# --- Fraud ------------------------------------------------------------------------
# weight = risk points per occurrence; reliability LOW never counts towards a block.
FRAUD_RULES = {
    "gps_impossible_speed": {"weight": 10, "reliability": "LOW", "ttl_hours": 24,
                             "max_kmh": 250, "min_km": 1.0},
    "implausible_trip_speed": {"weight": 25, "reliability": "MEDIUM", "ttl_hours": 72,
                               "max_avg_kmh": 130, "min_km": 2.0},
    "rider_rapid_cancellations": {"weight": 15, "reliability": "MEDIUM", "ttl_hours": 24,
                                  "threshold": 4, "window_hours": 1},
    "repeated_rider_driver_pair": {"weight": 20, "reliability": "MEDIUM", "ttl_hours": 168,
                                   "threshold": 4, "window_hours": 24},
    "payment_failed": {"weight": 15, "reliability": "HIGH", "ttl_hours": 72},
    "repeated_payment_failures": {"weight": 30, "reliability": "HIGH", "ttl_hours": 72,
                                  "threshold": 3, "window_hours": 24},
    "card_testing": {"threshold": 5, "window_hours": 1},  # synchronous hard limit
}  # fmt: skip
FRAUD_POLICY = {
    "review_score": 40,
    "block_score": 70,
    "min_distinct_reliable_signals": 2,
    "max_points_per_signal": 30,
    # If the check itself fails: allow (True) or refuse (False) the action.
    "fail_open": {"trip_request": True, "add_payment_method": False},
}
# Not the rider's doing: never a fraud signal.
FRAUD_IGNORED_FAILURE_CODES = ["provider_unavailable", "no_payment_method", "invalid_request"]

# --- Notifications ------------------------------------------------------------------------
NOTIFICATIONS_PROVIDER = env("NOTIFICATIONS_PROVIDER", default="sandbox")
NOTIFICATIONS_MAX_ATTEMPTS = env.int("NOTIFICATIONS_MAX_ATTEMPTS", default=5)
NOTIFICATIONS_RETRY_BASE_SECONDS = env.float("NOTIFICATIONS_RETRY_BASE_SECONDS", default=5)
NOTIFICATIONS_RETRY_MAX_SECONDS = env.float("NOTIFICATIONS_RETRY_MAX_SECONDS", default=300)
NOTIFICATIONS_LEASE_SECONDS = env.int("NOTIFICATIONS_LEASE_SECONDS", default=60)
NOTIFICATIONS_WORKER_BATCH = env.int("NOTIFICATIONS_WORKER_BATCH", default=50)
NOTIFICATIONS_WORKER_POLL_SECONDS = env.float("NOTIFICATIONS_WORKER_POLL_SECONDS", default=1.0)

# --- Real-time WebSockets (see docs/realtime.md) -----------------------------
# "redis" (required with more than one WebSocket instance) or "memory" (tests, one process).
CHANNEL_LAYER_BACKEND = env("CHANNEL_LAYER_BACKEND", default="redis")
REALTIME_HEARTBEAT_INTERVAL_SECONDS = env.float("REALTIME_HEARTBEAT_INTERVAL_SECONDS", default=25)
REALTIME_HEARTBEAT_TIMEOUT_SECONDS = env.float("REALTIME_HEARTBEAT_TIMEOUT_SECONDS", default=60)
REALTIME_MAX_MESSAGE_BYTES = env.int("REALTIME_MAX_MESSAGE_BYTES", default=4096)
REALTIME_APPROACHING_RADIUS_M = env.float("REALTIME_APPROACHING_RADIUS_M", default=300)
REALTIME_ALLOWED_ORIGINS = env.list("REALTIME_ALLOWED_ORIGINS", default=CORS_ALLOWED_ORIGINS)
REALTIME_ALLOW_MISSING_ORIGIN = env.bool("REALTIME_ALLOW_MISSING_ORIGIN", default=True)
# Inbound backpressure per connection (token bucket): excess messages are refused,
# and a client that keeps flooding is disconnected (close code 4429).
REALTIME_INBOUND_RATE_PER_SECOND = env.float("REALTIME_INBOUND_RATE_PER_SECOND", default=5)
REALTIME_INBOUND_BURST = env.int("REALTIME_INBOUND_BURST", default=20)
REALTIME_INBOUND_MAX_VIOLATIONS = env.int("REALTIME_INBOUND_MAX_VIOLATIONS", default=20)
if CHANNEL_LAYER_BACKEND == "redis":
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [REDIS_URL],
                "prefix": f"{REDIS_KEY_PREFIX}:ws:",
                "capacity": 1000,  # per-channel buffer; overflow drops for slow clients
                "expiry": 60,
                # Memberships of crashed instances vanish after this; live consumers
                # re-add their groups on every heartbeat.
                "group_expiry": 300,
            },
        }
    }
else:
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# --- Observability ----------------------------------------------------------
METRICS_BACKEND = env(
    "METRICS_BACKEND",
    default="apps.common.metrics.PrometheusMetrics"
    if IS_DEPLOYED
    else "apps.common.metrics.LoggingMetrics",
)
METRICS_TOKEN = env("METRICS_TOKEN", default="")  # bearer token Prometheus scrapes with
METRICS_PORT = env.int("METRICS_PORT", default=0)  # workers: serve /metrics on this port
METRICS_DROPPED_LABELS = ["zone"]  # unbounded label values never reach Prometheus
SLOW_REQUEST_MS = env.int("SLOW_REQUEST_MS", default=1000)
OPS_MONITOR_INTERVAL_SECONDS = env.float("OPS_MONITOR_INTERVAL_SECONDS", default=15)
SCHEDULER_TICK_SECONDS = env.float("SCHEDULER_TICK_SECONDS", default=10)
SENTRY_DSN = env("SENTRY_DSN", default="")
SENTRY_TRACES_SAMPLE_RATE = env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0)

# --- Security hardening for non-debug deployments ---------------------------
# TLS terminates at the load balancer / ingress, which must send X-Forwarded-Proto.
# HTTPS and WSS only: the API and WebSockets carry bearer tokens.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
    # Kubelet probes and in-cluster Prometheus talk plain HTTP to the pod.
    SECURE_REDIRECT_EXEMPT = [r"^api/v1/health/", r"^metrics$"]
    SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
    SECURE_HSTS_PRELOAD = env.bool("DJANGO_SECURE_HSTS_PRELOAD", default=False)
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
    SESSION_COOKIE_SECURE = True  # admin session only; the API uses bearer tokens
    SESSION_COOKIE_HTTPONLY = True
    CSRF_COOKIE_SECURE = True
    X_FRAME_OPTIONS = "DENY"

# --- Logging (with redaction of credentials / tokens) -----------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redact": {"()": "apps.common.logging.RedactSensitiveDataFilter"},
        "request_id": {"()": "apps.common.logging.RequestIdFilter"},
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
            "filters": ["request_id", "redact"],
        },
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

from apps.common.observability import init_error_tracking  # noqa: E402

init_error_tracking()
