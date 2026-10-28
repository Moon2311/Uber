"""Explicit trip lifecycle. Every status change goes through `transition`."""

from django.utils import timezone

from apps.common.exceptions import ConflictError

from .models import Trip, TripStatus
from .signals import trip_status_changed

S = TripStatus

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    S.REQUESTED: {S.SEARCHING, S.CANCELLED},
    S.SEARCHING: {S.DRIVER_ASSIGNED, S.CANCELLED},
    S.DRIVER_ASSIGNED: {S.DRIVER_ARRIVING, S.SEARCHING, S.CANCELLED},
    S.DRIVER_ARRIVING: {S.IN_PROGRESS, S.CANCELLED},
    S.IN_PROGRESS: {S.COMPLETED},
    S.COMPLETED: set(),
    S.CANCELLED: set(),
}

_TIMESTAMP_FIELD = {
    S.DRIVER_ASSIGNED: "driver_assigned_at",
    S.IN_PROGRESS: "started_at",
    S.COMPLETED: "completed_at",
    S.CANCELLED: "cancelled_at",
}


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def transition(trip: Trip, target: str, **changes) -> Trip:
    """Apply a status change to a row that the caller has locked (select_for_update)."""
    if not can_transition(trip.status, target):
        raise ConflictError(f"Cannot change trip status from {trip.status} to {target}.")
    previous = trip.status
    trip.status = target
    update_fields = ["status", "updated_at"]
    if field := _TIMESTAMP_FIELD.get(target):
        setattr(trip, field, timezone.now())
        update_fields.append(field)
    for name, value in changes.items():
        setattr(trip, name, value)
        update_fields.append(name)
    trip.save(update_fields=update_fields)
    # Same transaction as the status change -> outbox (apps.events).
    trip_status_changed.send(
        sender=Trip,
        trip=trip,
        from_status=previous,
        to_status=target,
        reason=changes.get("cancellation_reason"),
    )
    return trip
