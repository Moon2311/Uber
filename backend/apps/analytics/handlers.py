"""Handlers for the `analytics` consumer group.

They are deliberately NOT idempotent on their own (counters increment): the
EventProcessor's ProcessedEvent ledger, written in the same transaction, is what
makes redelivery safe. Tests prove a duplicate never double-counts.
"""

from datetime import datetime
from decimal import Decimal

from django.db.models import F

from apps.events.consumer import PermanentError

from .models import DriverDailyStats, TripTimelineEntry


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _stats(driver_id: int, at: datetime) -> DriverDailyStats:
    stats, _ = DriverDailyStats.objects.get_or_create(driver_id=driver_id, day=at.date())
    return stats


def on_trip_status_changed(event):
    p = event.payload
    TripTimelineEntry.objects.create(
        event_id=event.event_id,
        trip_id=p["trip_id"],
        event_type=event.event_type,
        status=p["to_status"],
        occurred_at=_ts(p["changed_at"]),
    )


def on_ride_requested(event):
    p = event.payload
    TripTimelineEntry.objects.create(
        event_id=event.event_id,
        trip_id=p["trip_id"],
        event_type=event.event_type,
        status="REQUESTED",
        occurred_at=_ts(p["requested_at"]),
    )


def on_trip_completed(event):
    p = event.payload
    fare = Decimal(p["final_fare"] or p["estimated_fare"])
    if fare < 0:
        raise PermanentError("negative fare")
    completed = _ts(p["completed_at"])
    stats = _stats(p["driver_id"], completed)
    DriverDailyStats.objects.filter(pk=stats.pk).update(
        completed_trips=F("completed_trips") + 1,
        gross_estimated_fare=F("gross_estimated_fare") + fare,
    )


def on_driver_availability_changed(event):
    p = event.payload
    if p["status"] != "ONLINE":
        return
    stats = _stats(p["driver_id"], _ts(p["changed_at"]))
    DriverDailyStats.objects.filter(pk=stats.pk).update(went_online=F("went_online") + 1)


HANDLERS = {
    "RideRequested": on_ride_requested,
    "TripStatusChanged": on_trip_status_changed,
    "TripCompleted": on_trip_completed,
    "DriverAvailabilityChanged": on_driver_availability_changed,
}
