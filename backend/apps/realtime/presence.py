"""Small Redis-backed registries used by the delivery layer (never by business
logic): connection presence and the driver -> active-trip cache that lets GPS
pings be fanned out to riders without a PostgreSQL query per ping.

Everything here is best effort: Redis errors are swallowed (metric) and the
worst case is a missed ephemeral push, which clients recover from via snapshots.
"""

from __future__ import annotations

import json
import logging
import time

import redis
from django.conf import settings

from apps.common.metrics import metrics
from apps.common.redis import get_redis

logger = logging.getLogger("apps.realtime")


def _k(*parts) -> str:
    return ":".join([settings.REDIS_KEY_PREFIX, "{ws}", *map(str, parts)])


def _safe(fn, default=None):
    try:
        return fn()
    except redis.exceptions.RedisError as exc:
        metrics().increment("realtime.redis_error")
        logger.warning("realtime.redis_error", extra={"error": str(exc)})
        return default


# --- presence ---------------------------------------------------------------------------
def touch(user_id: int, connection_id: str) -> None:
    key = _k("presence", user_id)

    def run():
        pipe = get_redis().pipeline()
        pipe.hset(key, connection_id, int(time.time()))
        pipe.expire(key, settings.REALTIME_HEARTBEAT_TIMEOUT_SECONDS * 2)
        pipe.execute()

    _safe(run)


def remove(user_id: int, connection_id: str) -> None:
    _safe(lambda: get_redis().hdel(_k("presence", user_id), connection_id))


def connections(user_id: int) -> dict[str, int]:
    return _safe(
        lambda: {k: int(v) for k, v in get_redis().hgetall(_k("presence", user_id)).items()},
        {},
    )


def cleanup_stale(now: float | None = None) -> int:
    """Drop presence entries not refreshed within the heartbeat timeout (crashed
    instances never run disconnect()). Group memberships expire on their own via
    the channel layer's group_expiry."""
    now = now or time.time()
    cutoff = now - settings.REALTIME_HEARTBEAT_TIMEOUT_SECONDS

    def run():
        removed = 0
        client = get_redis()
        for key in client.scan_iter(match=_k("presence", "*"), count=500):
            stale = [c for c, seen in client.hgetall(key).items() if int(seen) < cutoff]
            if stale:
                removed += client.hdel(key, *stale)
        return removed

    return _safe(run, 0)


# --- driver -> active trip (for location fan-out) -------------------------------------------
def set_driver_trip(driver_id: int, info: dict) -> None:
    _safe(lambda: get_redis().set(_k("drvtrip", driver_id), json.dumps(info), ex=6 * 3600))


def clear_driver_trip(driver_id: int) -> None:
    _safe(lambda: get_redis().delete(_k("drvtrip", driver_id)))


def driver_trip(driver_id: int) -> dict | None:
    raw = _safe(lambda: get_redis().get(_k("drvtrip", driver_id)))
    return json.loads(raw) if raw else None


def once(name: str, ttl_seconds: int) -> bool:
    """True the first time `name` is seen within ttl (dedupes one-shot notices)."""
    return bool(_safe(lambda: get_redis().set(_k("once", name), 1, nx=True, ex=ttl_seconds)))
