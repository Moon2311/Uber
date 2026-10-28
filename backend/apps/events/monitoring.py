"""Event pipeline health: Kafka reachability, outbox backlog, consumer lag and
consumer liveness (heartbeats)."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.common.metrics import metrics

from .models import ConsumerHeartbeat
from .registry import CONSUMER_GROUPS, physical_group
from .relay import outbox_stats


def summary() -> dict:
    """Cheap check for /api/v1/health/ (no consumer-group calls)."""
    if not settings.KAFKA_ENABLED:
        return {"status": "disabled"}
    from .kafka import ping

    outbox = outbox_stats()
    kafka = ping(timeout=1.0)
    problems = []
    if kafka["status"] != "ok":
        problems.append("kafka_unreachable")
    if outbox["oldest_pending_age_s"] > settings.EVENTS_OUTBOX_MAX_AGE_SECONDS:
        problems.append("outbox_backlog")
    if outbox["failed"]:
        problems.append("outbox_failed_events")
    metrics().gauge("events.outbox.pending", outbox["pending"])
    metrics().gauge("events.outbox.oldest_pending_age_s", outbox["oldest_pending_age_s"])
    return {
        "status": "degraded" if problems else "ok",
        "problems": problems,
        "kafka": kafka,
        "outbox": outbox,
    }


def detailed() -> dict:
    """Full report incl. per-partition lag (for /api/v1/health/events/ and ops)."""
    out = summary()
    if out["status"] == "disabled":
        return out
    from .kafka import consumer_lag

    groups = {}
    stale_after = timezone.now() - timedelta(seconds=settings.EVENTS_CONSUMER_STALE_SECONDS)
    for name, cfg in CONSUMER_GROUPS.items():
        entry = {"instances": [], "alive": 0}
        for hb in ConsumerHeartbeat.objects.filter(consumer_group=name):
            alive = hb.last_seen >= stale_after
            entry["alive"] += alive
            entry["instances"].append(
                {"instance": hb.instance, "last_seen": hb.last_seen, "alive": alive,
                 "processed": hb.processed, "dead_lettered": hb.dead_lettered}
            )  # fmt: skip
        if out["kafka"]["status"] == "ok":
            try:
                lag = consumer_lag(physical_group(name), cfg["topics"])
                entry["total_lag"] = lag["total_lag"]
                entry["partitions"] = lag["partitions"]
                metrics().gauge("events.consumer.lag", lag["total_lag"], tags={"group": name})
            except Exception as exc:  # noqa: BLE001
                entry["lag_error"] = str(exc)[:200]
        if entry["alive"] == 0:
            out["problems"].append(f"consumer_down:{name}")
        if entry.get("total_lag", 0) > settings.EVENTS_CONSUMER_MAX_LAG:
            out["problems"].append(f"consumer_lag:{name}")
        groups[name] = entry
    out["consumer_groups"] = groups
    out["status"] = "degraded" if out["problems"] else "ok"
    return out
