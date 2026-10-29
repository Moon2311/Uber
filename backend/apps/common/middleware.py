"""Request correlation, latency / DB-time metrics and slow-request logging."""

import logging
import time

from django.conf import settings
from django.db import connections

from .context import bound_request_id
from .metrics import metrics

logger = logging.getLogger("apps.http")
HEADER = "X-Request-ID"


class _QueryTimer:
    def __init__(self):
        self.count = 0
        self.seconds = 0.0

    def __call__(self, execute, sql, params, many, context):
        start = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            self.count += 1
            self.seconds += time.perf_counter() - start


def _route(request) -> str:
    """Bounded label: the URL *pattern* (never the raw path with ids)."""
    match = getattr(request, "resolver_match", None)
    return f"/{match.route}" if match and match.route else "unmatched"


class RequestContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with bound_request_id(request.headers.get(HEADER)) as request_id:
            request.request_id = request_id
            timer = _QueryTimer()
            start = time.perf_counter()
            with connections["default"].execute_wrapper(timer):
                response = self.get_response(request)
            elapsed_ms = (time.perf_counter() - start) * 1000
            response[HEADER] = request_id
            route = _route(request)
            if route.startswith(("/api/v1/health", "/metrics")):
                return response  # probes/scrapes would drown the real traffic
            tags = {"method": request.method, "route": route,
                    "status": f"{response.status_code // 100}xx"}  # fmt: skip
            m = metrics()
            m.timing("http.request.latency_ms", elapsed_ms, tags=tags)
            m.timing("http.request.db_ms", timer.seconds * 1000, tags={"route": route})
            m.increment("http.request.db_queries", timer.count, tags={"route": route})
            if elapsed_ms >= settings.SLOW_REQUEST_MS or response.status_code >= 500:
                logger.warning(
                    "http.slow_or_failed",
                    extra={"method": request.method, "route": route,
                           "status": response.status_code, "ms": round(elapsed_ms, 1),
                           "db_queries": timer.count, "db_ms": round(timer.seconds * 1000, 1)},
                )  # fmt: skip
            return response
