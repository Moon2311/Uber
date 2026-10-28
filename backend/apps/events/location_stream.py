"""Sampled DriverLocationUpdated stream (NOT through the outbox).

GPS pings arrive every few seconds per driver. Publishing each one would flood
every consumer with data that is stale seconds later, so a fix is published only
when the driver's H3 cell changed OR LOCATION_EVENT_MIN_INTERVAL_SECONDS passed
since the last published fix for that driver. The gate is one atomic Redis
script shared by all processes. Consumers needing real-time positions read the
Redis hot store, not Kafka.

Best effort by design: if Redis or Kafka is down the event is skipped (metric),
the GPS update itself still succeeds.
"""

from __future__ import annotations

from django.conf import settings

from apps.common.metrics import metrics

from . import builders
from .topics import physical

# KEYS: gate key   ARGV: cell, ttl seconds   -> reason string or false
GATE_LUA = """
local last = redis.call('GET', KEYS[1])
if last == false then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2]); return 'interval'
end
if last ~= ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2]); return 'cell_changed'
end
return false
"""

_script_cache = {}


def _gate(driver_id: int, cell: str | None) -> str | None:
    import redis

    from apps.common.redis import get_redis

    client = get_redis()
    script = _script_cache.get(id(client))
    if script is None:
        script = _script_cache[id(client)] = client.register_script(GATE_LUA)
    key = f"{settings.REDIS_KEY_PREFIX}:{{evt}}:locgate:{driver_id}"
    try:
        reason = script(
            keys=[key], args=[cell or "-", settings.LOCATION_EVENT_MIN_INTERVAL_SECONDS]
        )
    except redis.exceptions.RedisError:
        metrics().increment("events.location.gate_unavailable")
        return None
    return reason or None


def publish_sampled_location(fix) -> bool:
    if not (settings.KAFKA_ENABLED and settings.LOCATION_EVENTS_ENABLED):
        return False
    reason = _gate(fix.driver_id, fix.h3_cell)
    if reason is None:
        metrics().increment("events.location.sampled_out")
        return False
    from .kafka import OutgoingMessage, get_publisher

    event = builders.driver_location_updated(fix, reason)
    sent = get_publisher().produce_nowait(
        OutgoingMessage(physical(event.topic), event.key, event.to_bytes(), event.headers())
    )
    metrics().increment("events.location.published" if sent else "events.location.dropped")
    return sent
