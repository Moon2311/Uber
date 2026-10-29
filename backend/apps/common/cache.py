"""Redis cache that fails OPEN.

The default cache only holds rate-limit counters. If Redis is unreachable, losing
throttling for a moment is far better than failing every API request, so errors
are swallowed (reads miss, writes are dropped), counted and logged at most once
per few seconds. Never use this backend for data whose loss matters.
"""

import logging
import time

from django.core.cache.backends.redis import RedisCache

from .metrics import metrics

logger = logging.getLogger("apps.common")
_last_log = [0.0]


def _degraded(op: str, exc: Exception, default):
    metrics().increment("cache.unavailable", tags={"op": op})
    if time.monotonic() - _last_log[0] > 5:
        _last_log[0] = time.monotonic()
        logger.warning("cache.unavailable", extra={"op": op, "error": type(exc).__name__})
    return default


def _guard(name, default=None):
    def method(self, *args, **kwargs):
        import redis

        try:
            return getattr(RedisCache, name)(self, *args, **kwargs)
        except (redis.exceptions.RedisError, OSError) as exc:
            return _degraded(name, exc, default)

    return method


class ResilientRedisCache(RedisCache):
    def get(self, key, default=None, version=None):
        import redis

        try:
            return super().get(key, default, version)
        except (redis.exceptions.RedisError, OSError) as exc:
            return _degraded("get", exc, default)  # the CALLER's default (e.g. [])

    set = _guard("set")
    add = _guard("add", True)
    delete = _guard("delete", False)
    get_many = _guard("get_many", {})
    set_many = _guard("set_many", [])
    incr = _guard("incr", 1)
    touch = _guard("touch", False)
    has_key = _guard("has_key", False)
    clear = _guard("clear")
