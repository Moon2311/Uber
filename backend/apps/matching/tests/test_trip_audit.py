"""The trip audit trail as seen through the real matching workflow: offer
rejection, expiry, assignment failure and driver release -> reassignment."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.events.models import OutboxEvent
from apps.matching.models import JobStatus, MatchingJob, OfferStatus, RideOffer
from apps.matching.offers import accept_offer, decline_offer, expire_offers
from apps.matching.state import NullReservationLocks
from apps.payments.models import PaymentRecord, PaymentStatus
from apps.trips.models import TripEvent, TripStatus
from apps.trips.services import release_trip_by_driver
from apps.trips.tests.conftest import assert_audit_chain

from .conftest import driver_at, new_trip, north, orchestrator

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("matching_env")]
LOCKS = NullReservationLocks()


def timeline(trip):
    return [
        (e.event_type, e.to_status or e.driver_id)
        for e in TripEvent.objects.filter(trip_id=trip.pk).order_by("id")
    ]


def pending(trip):
    return RideOffer.objects.get(trip=trip, status=OfferStatus.PENDING)


def test_rejection_expiry_then_acceptance_are_all_recorded():
    a, b, c = driver_at(*north(0.3)), driver_at(*north(0.6)), driver_at(*north(0.9))
    trip = new_trip()
    worker = orchestrator(locks="none")

    worker.run_once()
    decline_offer(offer_id=pending(trip).pk, driver=a, locks=LOCKS)  # rejection
    worker.run_once()
    RideOffer.objects.filter(pk=pending(trip).pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    expire_offers(locks=LOCKS)  # expiry
    worker.run_once()
    accept_offer(offer_id=pending(trip).pk, driver=c, locks=LOCKS)

    assert timeline(trip) == [
        ("STATUS_CHANGED", TripStatus.REQUESTED),
        ("STATUS_CHANGED", TripStatus.SEARCHING),
        ("OFFER_SENT", a.pk), ("OFFER_DECLINED", a.pk),
        ("OFFER_SENT", b.pk), ("OFFER_EXPIRED", b.pk),
        ("OFFER_SENT", c.pk),
        ("STATUS_CHANGED", TripStatus.DRIVER_ASSIGNED), ("OFFER_ACCEPTED", c.pk),  # same txn
    ]  # fmt: skip
    declined = TripEvent.objects.get(trip_id=trip.pk, event_type="OFFER_DECLINED")
    assert declined.actor_type == "DRIVER" and declined.actor_id == a.pk
    expired = TripEvent.objects.get(trip_id=trip.pk, event_type="OFFER_EXPIRED")
    assert expired.actor_type == "SYSTEM" and expired.reason == "timeout"
    closed = OutboxEvent.objects.filter(event_type="RideOfferClosed", key=str(trip.pk))
    assert sorted(e.payload["status"] for e in closed) == ["DECLINED", "EXPIRED"]
    assert_audit_chain(trip)


def test_assignment_failure_is_explicit_and_cancels_by_system(settings):
    settings.MATCHING_TIMEOUT_SECONDS = 0  # deadline already passed
    trip = new_trip()
    orchestrator(locks="none").run_once()
    trip.refresh_from_db()
    assert trip.status == TripStatus.CANCELLED and trip.cancelled_by == "SYSTEM"
    assert trip.cancellation_reason == "no_driver_available"
    failure = TripEvent.objects.get(trip_id=trip.pk, event_type="ASSIGNMENT_FAILED")
    assert failure.reason == "no_driver_available" and "offers_made" in failure.metadata
    assert PaymentRecord.objects.get(trip=trip).status == PaymentStatus.CANCELLED
    assert MatchingJob.objects.get(trip=trip).status == JobStatus.EXHAUSTED
    assert_audit_chain(trip)


def test_released_trip_is_reassigned_to_a_different_driver():
    first, second = driver_at(*north(0.3)), driver_at(*north(0.8))
    trip = new_trip()
    worker = orchestrator(locks="none")
    worker.run_once()
    accept_offer(offer_id=pending(trip).pk, driver=first, locks=LOCKS)

    release_trip_by_driver(trip_id=trip.pk, driver=first, reason="vehicle problem")
    [report] = worker.run_once()
    offer = pending(trip)
    assert report.outcome.value == "offered" and offer.driver_id == second.pk  # first excluded
    accept_offer(offer_id=offer.pk, driver=second, locks=LOCKS)

    trip.refresh_from_db()
    assert trip.status == TripStatus.DRIVER_ASSIGNED and trip.driver_id == second.pk
    statuses = [e.to_status for e in assert_audit_chain(trip)]
    assert statuses == [TripStatus.REQUESTED, TripStatus.SEARCHING, TripStatus.DRIVER_ASSIGNED,
                        TripStatus.SEARCHING, TripStatus.DRIVER_ASSIGNED]  # fmt: skip
    assert MatchingJob.objects.get(trip=trip).status == JobStatus.MATCHED
