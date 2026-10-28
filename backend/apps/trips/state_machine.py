"""Explicit trip lifecycle. Every status change goes through `transition`.

    REQUESTED -> SEARCHING -> DRIVER_ASSIGNED -> DRIVER_ARRIVING -> IN_PROGRESS -> COMPLETED
         \\            \\          |      ^             |
          \\            \\         |      +--(driver releases: re-search)
           +------------+--------+--------------------+--> CANCELLED

The table below is the single source of truth: each legal edge lists the actors
allowed to take it. A transition is legal only if (from, to) is listed AND the
actor is allowed. Offer rejection / expiry and assignment failure are not status
changes; they are recorded as TripEvents (see audit.py) and, for assignment
failure, end in SEARCHING -> CANCELLED by SYSTEM with reason no_driver_available.

Concurrency, in two layers:
  * callers lock the trip row (SELECT ... FOR UPDATE) to serialise work on it;
  * the write itself is a compare-and-set on (status, state_version). A caller
    that skipped the lock, or holds a stale copy, gets StaleTripState instead of
    silently overwriting a newer state.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.common.exceptions import ConflictError
from apps.common.metrics import metrics

from . import audit
from .models import ActorType, Trip, TripEventType, TripStatus
from .signals import trip_status_changed

S = TripStatus
A = ActorType


@dataclass(frozen=True)
class Actor:
    type: str
    id: int | None = None  # user id for riders/admins, driver profile id for drivers

    @classmethod
    def system(cls) -> Actor:
        return cls(A.SYSTEM)

    @classmethod
    def rider(cls, user) -> Actor:
        return cls(A.RIDER, user.pk)

    @classmethod
    def driver(cls, driver) -> Actor:
        return cls(A.DRIVER, driver.pk)


class InvalidTransition(ConflictError):
    default_code = "invalid_transition"


class StaleTripState(ConflictError):
    default_detail = "The trip was changed by someone else. Reload and retry."
    default_code = "stale_trip_state"


_ANY_OPERATOR = {A.SYSTEM, A.ADMIN}

TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    (S.REQUESTED, S.SEARCHING): frozenset({A.SYSTEM}),
    (S.REQUESTED, S.CANCELLED): frozenset({A.RIDER, *_ANY_OPERATOR}),
    (S.SEARCHING, S.DRIVER_ASSIGNED): frozenset({A.DRIVER}),  # accepting an offer
    (S.SEARCHING, S.CANCELLED): frozenset({A.RIDER, *_ANY_OPERATOR}),
    (S.DRIVER_ASSIGNED, S.DRIVER_ARRIVING): frozenset({A.DRIVER}),
    (S.DRIVER_ASSIGNED, S.SEARCHING): frozenset({A.DRIVER, A.ADMIN}),  # driver releases
    (S.DRIVER_ASSIGNED, S.CANCELLED): frozenset({A.RIDER, *_ANY_OPERATOR}),
    (S.DRIVER_ARRIVING, S.IN_PROGRESS): frozenset({A.DRIVER}),
    (S.DRIVER_ARRIVING, S.SEARCHING): frozenset({A.DRIVER, A.ADMIN}),
    (S.DRIVER_ARRIVING, S.CANCELLED): frozenset({A.RIDER, *_ANY_OPERATOR}),
    (S.IN_PROGRESS, S.COMPLETED): frozenset({A.DRIVER, A.ADMIN}),
}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {status: set() for status in S.values}
for _from, _to in TRANSITIONS:
    ALLOWED_TRANSITIONS[_from].add(_to)

# Timestamp recorded the FIRST time a trip enters a status.
_TIMESTAMP_FIELD = {
    S.SEARCHING: "search_started_at",
    S.DRIVER_ASSIGNED: "driver_assigned_at",
    S.DRIVER_ARRIVING: "driver_arrived_at",
    S.IN_PROGRESS: "started_at",
    S.COMPLETED: "completed_at",
    S.CANCELLED: "cancelled_at",
}
# Fields a transition may change besides status/version/timestamps.
_CHANGEABLE = {"driver", "cancellation_reason"}


def can_transition(current: str, target: str, actor_type: str | None = None) -> bool:
    actors = TRANSITIONS.get((current, target))
    return actors is not None and (actor_type is None or actor_type in actors)


def transition(
    trip: Trip,
    target: str,
    *,
    actor: Actor,
    reason: str = "",
    expected_version: int | None = None,
    metadata: dict | None = None,
    **changes,
) -> Trip:
    """Apply a status change inside the caller's transaction and record it.

    Raises InvalidTransition (illegal edge / actor), StaleTripState (the row moved
    on since `trip` was read, or `expected_version` does not match)."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("state_machine.transition() must run inside transaction.atomic()")
    if not can_transition(trip.status, target):
        raise InvalidTransition(f"Cannot change trip status from {trip.status} to {target}.")
    if not can_transition(trip.status, target, actor.type):
        raise InvalidTransition(
            f"{actor.type.title()} cannot change trip status from {trip.status} to {target}."
        )
    if unknown := set(changes) - _CHANGEABLE:
        raise ValueError(f"transition() cannot change {sorted(unknown)}")
    if expected_version is not None and expected_version != trip.state_version:
        raise StaleTripState()

    previous, previous_driver_id = trip.status, trip.driver_id
    now = timezone.now()
    values = {"status": target, "state_version": trip.state_version + 1, "updated_at": now}
    if (field := _TIMESTAMP_FIELD.get(target)) and getattr(trip, field) is None:
        values[field] = now
    if target == S.CANCELLED:
        values["cancelled_by"] = actor.type
        values["cancellation_reason"] = reason
    if target == S.SEARCHING and previous != S.REQUESTED:
        # Driver released the trip: it goes back to matching without a driver.
        values.update(driver=None, driver_assigned_at=None, driver_arrived_at=None)
    values.update(changes)

    # Compare-and-set: succeeds only if nobody changed the row since we read it.
    updated = Trip.objects.filter(
        pk=trip.pk, status=previous, state_version=trip.state_version
    ).update(**values)
    if updated != 1:
        metrics().increment("trip.transition.stale", tags={"to": target})
        raise StaleTripState()
    for name, value in values.items():
        setattr(trip, name, value)

    event = audit.record(
        trip,
        TripEventType.STATUS_CHANGED,
        actor=actor,
        from_status=previous,
        to_status=target,
        state_version=trip.state_version,
        driver_id=trip.driver_id or previous_driver_id,
        reason=reason,
        metadata=metadata,
        occurred_at=now,
    )
    # Same transaction as the status change -> outbox (apps.events), matching,
    # payments. The audit event id doubles as the outbox event id.
    trip_status_changed.send(
        sender=Trip,
        trip=trip,
        from_status=previous,
        to_status=target,
        reason=reason or None,
        actor_type=actor.type,
        previous_driver_id=previous_driver_id,
        event_id=event.event_id,
    )
    metrics().increment("trip.transition", tags={"from": previous, "to": target})
    return trip
