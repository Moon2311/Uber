from django.db import transaction

from apps.accounts.models import User
from apps.common.exceptions import ConflictError
from apps.payments.services import cancel_payment_for_trip, create_pending_payment
from apps.pricing.services import Point, get_fare_estimator

from . import state_machine
from .models import Trip, TripStatus

RIDER_CANCELLABLE = {
    TripStatus.REQUESTED,
    TripStatus.SEARCHING,
    TripStatus.DRIVER_ASSIGNED,
    TripStatus.DRIVER_ARRIVING,
}


@transaction.atomic
def request_trip(*, rider: User, pickup: dict, dropoff: dict, ride_category: str) -> Trip:
    # Serialise concurrent requests from the same rider on the user row.
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
        status=TripStatus.REQUESTED,  # matching (step 3+) moves it to SEARCHING
        estimated_fare=estimate.amount,
        estimated_distance_km=estimate.distance_km,
        estimated_duration_minutes=estimate.duration_minutes,
        currency=estimate.currency,
    )
    create_pending_payment(trip=trip)
    return trip


@transaction.atomic
def cancel_trip_by_rider(*, trip_id, rider: User, reason: str = "") -> Trip:
    trip = Trip.objects.select_for_update().filter(rider=rider, pk=trip_id).first()
    if trip is None:
        raise Trip.DoesNotExist
    if trip.status not in RIDER_CANCELLABLE:
        raise ConflictError(f"A trip in status {trip.status} cannot be cancelled.")
    state_machine.transition(trip, TripStatus.CANCELLED, cancellation_reason=reason)
    cancel_payment_for_trip(trip=trip)
    return trip
