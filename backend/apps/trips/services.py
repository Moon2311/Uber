"""Trip use cases. Each is one PostgreSQL transaction that locks the trip row,
validates, and delegates the status change to state_machine.transition.

Side effects in other modules (payment record, matching job, outbox events) are
attached to the trip signals and run INSIDE the same transaction, so a trip and
its dependants commit or roll back together: there are no dual writes here.
"""

from django.db import transaction

from apps.accounts.models import User
from apps.common.exceptions import ConflictError
from apps.pricing.services import Point, get_fare_estimator

from . import audit, state_machine
from .models import Trip, TripEventType, TripStatus
from .signals import trip_requested
from .state_machine import Actor


@transaction.atomic
def request_trip(*, rider: User, pickup: dict, dropoff: dict, ride_category: str) -> Trip:
    # Serialise concurrent requests from the same rider on the user row; the partial
    # unique index trip_one_active_per_rider is the final guard if anything slips by.
    User.objects.select_for_update().get(pk=rider.pk)
    if Trip.objects.filter(rider=rider).active().exists():
        raise ConflictError("You already have an active trip.")

    estimate = get_fare_estimator().estimate(
        pickup=Point(pickup["lat"], pickup["lng"]),
        dropoff=Point(dropoff["lat"], dropoff["lng"]),
        category=ride_category,
    )
    trip = Trip.objects.create(
        rider=rider,
        pickup_latitude=pickup["lat"],
        pickup_longitude=pickup["lng"],
        pickup_address=pickup.get("address", ""),
        dropoff_latitude=dropoff["lat"],
        dropoff_longitude=dropoff["lng"],
        dropoff_address=dropoff.get("address", ""),
        ride_category=ride_category,
        status=TripStatus.REQUESTED,  # the matching worker moves it to SEARCHING
        estimated_fare=estimate.amount,
        estimated_distance_km=estimate.distance_km,
        estimated_duration_minutes=estimate.duration_minutes,
        currency=estimate.currency,
    )
    actor = Actor.rider(rider)
    event = audit.record(
        trip, TripEventType.STATUS_CHANGED, actor=actor, to_status=TripStatus.REQUESTED,
        state_version=0, occurred_at=trip.created_at,
    )  # fmt: skip
    # Receivers (same transaction): pending payment, matching job, outbox.
    trip_requested.send(sender=Trip, trip=trip, actor_type=actor.type, event_id=event.event_id)
    return trip


def _locked(trip_id, **owner) -> Trip:
    trip = Trip.objects.select_for_update().filter(pk=trip_id, **owner).first()
    if trip is None:
        raise Trip.DoesNotExist
    return trip


@transaction.atomic
def cancel_trip_by_rider(
    *, trip_id, rider: User, reason: str = "", expected_version: int | None = None
) -> Trip:
    """Idempotent: cancelling an already-cancelled trip returns it unchanged."""
    trip = _locked(trip_id, rider=rider)
    if trip.status == TripStatus.CANCELLED:
        return trip
    actor = Actor.rider(rider)
    if not state_machine.can_transition(trip.status, TripStatus.CANCELLED, actor.type):
        raise ConflictError(f"A trip in status {trip.status} cannot be cancelled.")
    return state_machine.transition(
        trip, TripStatus.CANCELLED, actor=actor, reason=reason, expected_version=expected_version
    )


# --- driver-side milestones (assigned driver only) ----------------------------------
DRIVER_MILESTONES = {
    "arrive": (TripStatus.DRIVER_ASSIGNED, TripStatus.DRIVER_ARRIVING),
    "start": (TripStatus.DRIVER_ARRIVING, TripStatus.IN_PROGRESS),
    "complete": (TripStatus.IN_PROGRESS, TripStatus.COMPLETED),
}


@transaction.atomic
def advance_trip_by_driver(
    *, trip_id, driver, milestone: str, expected_version: int | None = None
) -> Trip:
    """Assigned driver moves the trip forward. Idempotent: repeating a milestone that
    already happened returns the trip unchanged (checked before the version, so a
    retry after a lost response succeeds)."""
    expected, target = DRIVER_MILESTONES[milestone]
    trip = _locked(trip_id, driver=driver)
    if trip.status == target:
        return trip
    if trip.status != expected:
        raise ConflictError(f"Cannot {milestone} a trip in status {trip.status}.")
    return state_machine.transition(
        trip, target, actor=Actor.driver(driver), expected_version=expected_version
    )


@transaction.atomic
def release_trip_by_driver(
    *, trip_id, driver, reason: str = "", expected_version: int | None = None
) -> Trip:
    """The assigned driver backs out before pickup. The trip goes back to SEARCHING
    (not CANCELLED: the rider still wants a ride) and matching resumes without this
    driver. Retrying returns the trip without error once this driver is off it."""
    trip = Trip.objects.select_for_update().filter(pk=trip_id).first()
    if trip is None:
        raise Trip.DoesNotExist
    if trip.driver_id != driver.pk:
        # Already released by this driver (retry) -> visible as an event, else 404.
        released = trip.events.filter(
            to_status=TripStatus.SEARCHING, actor_type="DRIVER", actor_id=driver.pk
        ).exists()
        if released:
            return trip
        raise Trip.DoesNotExist
    if trip.status not in (TripStatus.DRIVER_ASSIGNED, TripStatus.DRIVER_ARRIVING):
        raise ConflictError(f"A trip in status {trip.status} cannot be released.")
    return state_machine.transition(
        trip,
        TripStatus.SEARCHING,
        actor=Actor.driver(driver),
        reason=reason or "driver_released",
        expected_version=expected_version,
    )
