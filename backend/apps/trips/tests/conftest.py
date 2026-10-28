from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.matching.models import OfferStatus, RideOffer
from apps.trips import state_machine
from apps.trips.models import Trip, TripEvent, TripEventType, TripStatus
from apps.trips.services import request_trip
from apps.trips.state_machine import Actor
from conftest import make_rider

PICKUP = {"lat": Decimal("37.774900"), "lng": Decimal("-122.419400"), "address": "Market St"}
DROPOFF = {"lat": Decimal("37.804400"), "lng": Decimal("-122.271200"), "address": "Oakland"}
S = TripStatus


def new_trip(rider=None) -> Trip:
    return request_trip(
        rider=rider or make_rider(), pickup=PICKUP, dropoff=DROPOFF, ride_category="ECONOMY"
    )


def advance(trip: Trip, *targets, driver=None) -> Trip:
    """Walk a trip through legal transitions, each with the actor that may take it."""
    for target in targets:
        with transaction.atomic():
            locked = Trip.objects.select_for_update().get(pk=trip.pk)
            if target == S.SEARCHING and locked.status == S.REQUESTED:
                actor = Actor.system()
            elif target in (S.DRIVER_ASSIGNED, S.DRIVER_ARRIVING, S.IN_PROGRESS, S.COMPLETED):
                actor = Actor.driver(driver)
            else:
                actor = Actor.system()
            extra = {"driver": driver} if target == S.DRIVER_ASSIGNED else {}
            state_machine.transition(locked, target, actor=actor, **extra)
    trip.refresh_from_db()
    return trip


def pending_offer(trip: Trip, driver, *, ttl=15) -> RideOffer:
    """A PENDING offer as the matcher would create it (without the search)."""
    return RideOffer.objects.create(
        trip=trip, driver=driver, status=OfferStatus.PENDING,
        expires_at=timezone.now() + timedelta(seconds=ttl), rank=1, score=1.0,
        pickup_eta_seconds=120, eta_source="test", eta_is_fallback=True, straight_line_km=0.5,
        grid_distance=1,
    )  # fmt: skip


def status_events(trip) -> list[TripEvent]:
    return list(
        TripEvent.objects.filter(trip_id=trip.pk, event_type=TripEventType.STATUS_CHANGED).order_by(
            "state_version"
        )
    )


def assert_audit_chain(trip: Trip) -> list[TripEvent]:
    """The status history is gap-free (0..N), each step starts where the previous
    ended, and the last step is the row's current state."""
    trip.refresh_from_db()
    events = status_events(trip)
    assert [e.state_version for e in events] == list(range(trip.state_version + 1)), events
    assert events[0].from_status == "" and events[0].to_status == S.REQUESTED
    for before, after in zip(events, events[1:], strict=False):
        assert after.from_status == before.to_status, (before, after)
        assert state_machine.can_transition(after.from_status, after.to_status, after.actor_type)
    assert events[-1].to_status == trip.status
    return events
