"""System-level gauges collected periodically by `run_ops_monitor`.

Request-path metrics (latency, errors, matching rounds, ETA calls...) are emitted
where they happen. These are STATE metrics that nobody emits on their own:
backlogs, freshness, lag, database health. Each probe is isolated: one failing
probe (e.g. Kafka down) never hides the others.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .metrics import metrics

logger = logging.getLogger("apps.common")


def _locations(now) -> dict:
    """Online drivers vs drivers with a fresh fix in the live store."""
    from apps.drivers.models import AvailabilityStatus, DriverProfile
    from apps.locations.service import get_location_service
    from apps.locations.stores import BoundingBox

    online = DriverProfile.objects.filter(availability_status=AvailabilityStatus.ONLINE).count()
    world = BoundingBox(-85.0, 85.0, -179.9, 179.9)
    fresh_after = now - timedelta(seconds=settings.DRIVER_LOCATION_STALE_SECONDS)
    fixes = get_location_service().store.find_in_bbox(world, fresh_after=now - timedelta(hours=1))
    fresh = sum(1 for f in fixes if f.recorded_at >= fresh_after)
    ages = sorted((now - f.recorded_at).total_seconds() for f in fixes)
    p95 = ages[int(0.95 * (len(ages) - 1))] if ages else 0.0
    return {"drivers.online": online, "drivers.fresh_location": fresh,
            "drivers.online_without_fresh_location": max(0, online - fresh),
            "location.age_p95_s": round(p95, 1)}  # fmt: skip


def _matching(now) -> dict:
    from apps.matching.models import JobStatus, MatchingJob, OfferStatus, RideOffer
    from apps.trips.models import Trip, TripStatus

    active = MatchingJob.objects.filter(status=JobStatus.ACTIVE)
    oldest = active.order_by("created_at").values_list("created_at", flat=True).first()
    return {
        "matching.jobs_active": active.count(),
        "matching.oldest_job_age_s": (now - oldest).total_seconds() if oldest else 0.0,
        "matching.offers_pending": RideOffer.objects.filter(status=OfferStatus.PENDING).count(),
        "trips.searching": Trip.objects.filter(status=TripStatus.SEARCHING).count(),
    }


def _events(now) -> dict:
    from apps.events.monitoring import detailed

    report = detailed()  # also sets events.consumer.lag{group} gauges
    out = {"events.problems": len(report.get("problems", []))}
    if outbox := report.get("outbox"):
        out["events.outbox.pending"] = outbox["pending"]
        out["events.outbox.failed"] = outbox["failed"]
        out["events.outbox.oldest_pending_age_s"] = outbox["oldest_pending_age_s"]
    return out


def _payments(now) -> dict:
    from apps.payments.models import OPEN_ATTEMPT_STATUSES, PaymentAttempt, PaymentRecord

    hour_ago = now - timedelta(hours=1)
    return {
        "payments.needs_review": PaymentRecord.objects.filter(needs_review=True).count(),
        "payments.open_attempts": PaymentAttempt.objects.filter(
            status__in=OPEN_ATTEMPT_STATUSES
        ).count(),  # fmt: skip
        "payments.failed_last_hour": PaymentRecord.objects.filter(
            status="FAILED", failed_at__gte=hour_ago
        ).count(),  # fmt: skip
        "payments.paid_last_hour": PaymentRecord.objects.filter(
            status="PAID", paid_at__gte=hour_ago
        ).count(),  # fmt: skip
    }


def _notifications(now) -> dict:
    from apps.notifications.models import Notification, NotificationStatus

    pending = Notification.objects.filter(status=NotificationStatus.PENDING)
    oldest = pending.order_by("created_at").values_list("created_at", flat=True).first()
    return {
        "notifications.pending": pending.count(),
        "notifications.oldest_pending_age_s": (now - oldest).total_seconds() if oldest else 0.0,
        "notifications.failed_last_hour": Notification.objects.filter(
            status=NotificationStatus.FAILED, created_at__gte=now - timedelta(hours=1)
        ).count(),  # fmt: skip
    }


def _database(now) -> dict:
    with connection.cursor() as c:
        c.execute("""
            SELECT count(*),
                   count(*) FILTER (WHERE state = 'active'),
                   count(*) FILTER (WHERE wait_event_type = 'Lock'),
                   coalesce(max(extract(epoch FROM now() - xact_start))
                            FILTER (WHERE state <> 'idle'), 0)
            FROM pg_stat_activity WHERE datname = current_database()
        """)
        total, active, waiting, longest_tx = c.fetchone()
        c.execute("""
            SELECT deadlocks, blks_hit::float / greatest(blks_hit + blks_read, 1),
                   pg_database_size(current_database())
            FROM pg_stat_database WHERE datname = current_database()
        """)
        deadlocks, hit_ratio, size = c.fetchone()
        c.execute("SELECT setting::int FROM pg_settings WHERE name = 'max_connections'")
        (max_conn,) = c.fetchone()
    return {
        "db.connections": total, "db.connections_active": active,
        "db.connections_waiting_on_lock": waiting, "db.max_connections": max_conn,
        "db.longest_transaction_s": float(longest_tx), "db.deadlocks_total": deadlocks,
        "db.cache_hit_ratio": round(hit_ratio, 4), "db.size_bytes": size,
    }  # fmt: skip


PROBES = {
    "locations": _locations,
    "matching": _matching,
    "events": _events,
    "payments": _payments,
    "notifications": _notifications,
    "database": _database,
}


def collect(now=None) -> dict:
    now = now or timezone.now()
    values, m = {}, metrics()
    for name, probe in PROBES.items():
        try:
            result = probe(now)
        except Exception as exc:  # noqa: BLE001 - one broken probe must not hide the rest
            logger.warning("ops.probe_failed", extra={"probe": name, "error": repr(exc)[:200]})
            m.increment("ops.probe_failed", tags={"probe": name})
            continue
        for key, value in result.items():
            m.gauge(key, float(value))
        values.update(result)
    return values
