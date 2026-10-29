"""Deployment checks: `manage.py check --deploy` (CI and the migrate job run it with
the target environment's settings). Errors block the deploy; warnings are printed.

These complement Django's own --deploy checks with this application's risks.
"""

from urllib.parse import urlparse

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

WEAK = {"change-me", "changeme", "secret", "dev", "test"}


def _weak(value: str, min_len: int = 32) -> bool:
    return not value or value.lower() in WEAK or len(value) < min_len


@register(Tags.security, deploy=True)
def deployment_checks(app_configs, **kwargs):
    out = []
    E = lambda msg, id_, hint=None: out.append(Error(msg, hint=hint, id=f"rh.E{id_}"))  # noqa: E731
    W = lambda msg, id_, hint=None: out.append(Warning(msg, hint=hint, id=f"rh.W{id_}"))  # noqa: E731

    if settings.APP_ENV not in ("staging", "production"):
        W(f"APP_ENV={settings.APP_ENV}: deployment checks assume staging/production.", "001")
    if settings.DEBUG:
        E("DEBUG must be off.", "002")
    if _weak(settings.SECRET_KEY, 50):
        E("DJANGO_SECRET_KEY is missing, a placeholder or shorter than 50 chars.", "003")
    jwt_key = settings.SIMPLE_JWT.get("SIGNING_KEY", "")
    if jwt_key == settings.SECRET_KEY or _weak(jwt_key):
        E("JWT_SIGNING_KEY must be set, strong, and different from DJANGO_SECRET_KEY.", "004",
          "Rotating one must not force rotating the other.")  # fmt: skip
    if not settings.ALLOWED_HOSTS or "*" in settings.ALLOWED_HOSTS:
        E("DJANGO_ALLOWED_HOSTS must list the real host names (no '*').", "005")
    for origin in [*settings.CORS_ALLOWED_ORIGINS, *settings.REALTIME_ALLOWED_ORIGINS]:
        if urlparse(origin).scheme != "https":
            E(f"Origin {origin!r} is not https.", "006", "Browsers must use HTTPS/WSS only.")
    if settings.CACHES["default"]["BACKEND"].endswith("LocMemCache"):
        E("Rate limiting uses a per-process cache.", "007",
          "Set CACHE_BACKEND=redis so limits hold across workers and replicas.")  # fmt: skip
    if settings.CHANNEL_LAYERS["default"]["BACKEND"].endswith("InMemoryChannelLayer"):
        E("WebSockets use the in-memory channel layer.", "008", "Set CHANNEL_LAYER_BACKEND=redis.")
    if settings.REST_FRAMEWORK.get("NUM_PROXIES") is None:
        W("NUM_PROXIES is unset: behind a load balancer every client shares one rate-limit "
          "bucket (or spoofed X-Forwarded-For is trusted).", "009")  # fmt: skip
    if not settings.METRICS_TOKEN:
        E("METRICS_TOKEN is unset: /metrics would be refused (or open).", "010")
    if "Prometheus" not in settings.METRICS_BACKEND:
        W("METRICS_BACKEND is not Prometheus: no metrics will be scraped.", "011")
    secrets = [s for s in settings.PAYMENTS_WEBHOOK_SECRETS if s]
    if not secrets or any(_weak(s) for s in secrets):
        E("PAYMENTS_WEBHOOK_SECRETS missing or weak (>= 32 chars each).", "012")
    if settings.PAYMENTS_PROVIDER == "sandbox":
        msg = "Payments use the SANDBOX provider: no real money can be collected."
        if settings.PAYMENTS_SANDBOX_ALLOWED:
            W(msg + " (explicitly allowed)", "013")
        else:
            E(msg, "013", "Integrate a real PaymentProvider before production.")
    if settings.NOTIFICATIONS_PROVIDER == "sandbox":
        W("Push notifications use the sandbox provider (logged, not delivered).", "014")
    db = settings.DATABASES["default"]
    if db.get("OPTIONS", {}).get("sslmode") not in ("require", "verify-ca", "verify-full"):
        W("Database connection does not require TLS (add ?sslmode=require to DATABASE_URL).",
          "015")  # fmt: skip
    if settings.REDIS_URL.startswith("redis://") and settings.APP_ENV == "production":
        W("REDIS_URL is not TLS (rediss://).", "016")
    if settings.API_DOCS_ENABLED:
        W("Interactive API docs are enabled.", "017")
    if settings.ADMIN_URL_PATH == "admin/":
        W("Admin is at the default /admin/ path (set ADMIN_URL_PATH).", "018")
    if not settings.SENTRY_DSN:
        W("SENTRY_DSN unset: unhandled errors are only in logs.", "019")
    if settings.PRICING_SURGE_ENABLED:
        W("Surge pricing is enabled: confirm legal review for every market served "
          "(docs/pricing-payments.md).", "020")  # fmt: skip
    return out
