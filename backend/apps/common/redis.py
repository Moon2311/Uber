"""Shared Redis client factory and health check.

Reconnect behaviour:
* redis-py's connection pool transparently opens a new connection when one is
  dropped; `retry` re-runs a failed command on ConnectionError/TimeoutError with
  exponential backoff (REDIS_RETRY_ATTEMPTS times, capped at ~0.5 s total).
* `health_check_interval` PINGs idle connections before reuse, so a connection
  silently killed by a server restart or proxy is replaced instead of failing.
* Short socket/connect timeouts keep a dead Redis from stalling request threads;
  callers (FailoverLocationStore) add a circuit breaker on top.
"""

from __future__ import annotations

import time
from functools import lru_cache

import redis
from django.conf import settings
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

# Errors meaning "Redis is unreachable or not serving right now" (as opposed to a
# bug such as a Lua script error, which must surface).
UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    redis.exceptions.ConnectionError,
    redis.exceptions.TimeoutError,
    redis.exceptions.BusyLoadingError,
    redis.exceptions.ReadOnlyError,
    redis.exceptions.ClusterDownError,
)
OOM_MARKER = "OOM"


def is_unavailable_error(exc: BaseException) -> bool:
    if isinstance(exc, UNAVAILABLE_ERRORS):
        return True
    # maxmemory reached with noeviction: writes rejected -> treat as unavailable.
    return isinstance(exc, redis.exceptions.ResponseError) and str(exc).startswith(OOM_MARKER)


@lru_cache(maxsize=8)
def _client(url, socket_timeout, connect_timeout, retries, health_interval) -> redis.Redis:
    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_timeout=socket_timeout,
        socket_connect_timeout=connect_timeout,
        retry=Retry(ExponentialBackoff(cap=0.25, base=0.02), retries),
        retry_on_error=[redis.exceptions.ConnectionError, redis.exceptions.TimeoutError],
        health_check_interval=health_interval,
        client_name="ride-hailing-backend",
    )


def get_redis() -> redis.Redis:
    return _client(
        settings.REDIS_URL,
        settings.REDIS_SOCKET_TIMEOUT,
        settings.REDIS_CONNECT_TIMEOUT,
        settings.REDIS_RETRY_ATTEMPTS,
        settings.REDIS_HEALTH_CHECK_INTERVAL,
    )


def redis_health(client: redis.Redis | None = None) -> dict:
    client = client or get_redis()
    start = time.perf_counter()
    try:
        client.ping()
    except redis.exceptions.RedisError as exc:
        return {"status": "unavailable", "error": type(exc).__name__}
    return {"status": "ok", "latency_ms": round((time.perf_counter() - start) * 1000, 2)}
