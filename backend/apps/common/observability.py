"""Prometheus exposition for web processes (/metrics) and workers (own port),
and optional error tracking (Sentry)."""

from __future__ import annotations

import hmac
import logging
import os

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden

logger = logging.getLogger("apps.common")


def _registry():
    from prometheus_client import REGISTRY, CollectorRegistry, multiprocess

    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return registry
    return REGISTRY


def metrics_view(request):
    """Scraped by Prometheus with `Authorization: Bearer <METRICS_TOKEN>`. Without a
    token configured it is only served in DEBUG (local development)."""
    token = settings.METRICS_TOKEN
    if token:
        sent = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(sent.encode(), token.encode()):
            return HttpResponseForbidden()
    elif not settings.DEBUG:
        return HttpResponseForbidden()
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return HttpResponse(generate_latest(_registry()), content_type=CONTENT_TYPE_LATEST)


def start_worker_metrics_server() -> None:
    """Long-running management commands call this; it serves /metrics on
    METRICS_PORT (cluster-internal) when the Prometheus backend is active."""
    port = settings.METRICS_PORT
    if not port or "Prometheus" not in settings.METRICS_BACKEND:
        return
    from prometheus_client import start_http_server

    try:
        start_http_server(port, registry=_registry())
        logger.info("metrics.server.started", extra={"port": port})
    except OSError as exc:  # port taken (e.g. two commands in one container)
        logger.warning("metrics.server.unavailable", extra={"port": port, "error": str(exc)})


def init_error_tracking() -> None:
    """Sentry (or any Sentry-protocol service, e.g. GlitchTip) when SENTRY_DSN is set.
    PII is never sent; messages are passed through the same redaction as logs."""
    dsn = settings.SENTRY_DSN
    if not dsn:
        return
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    from .context import get_request_id
    from .logging import redact

    def before_send(event, hint):
        if rid := get_request_id():
            event.setdefault("tags", {})["request_id"] = rid
        request = event.get("request") or {}
        request.pop("cookies", None)
        request.pop("data", None)  # bodies may hold tokens / personal data
        if headers := request.get("headers"):
            for h in list(headers):
                if h.lower() in ("authorization", "cookie", "payment-signature"):
                    headers[h] = "[REDACTED]"
        if msg := (event.get("logentry") or {}).get("message"):
            event["logentry"]["message"] = redact(msg)
        return event

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.APP_ENV,
        release=settings.APP_RELEASE or None,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        send_default_pii=False,
        integrations=[DjangoIntegration(), LoggingIntegration(event_level=logging.ERROR)],
        before_send=before_send,
    )
