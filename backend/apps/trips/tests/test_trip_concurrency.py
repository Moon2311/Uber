"""Concurrency: real threads, each with its own PostgreSQL connection, released
together by a barrier. Every scenario ends with invariant checks on the trip, its
audit chain, offers, matching job, payment record and outbox."""

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone

from apps.events.models import OutboxEvent
from apps.matching.models import JobStatus, MatchingJob, OfferStatus, RideOffer
from apps.matching.offers import accept_offer, expire_offers
from apps.matching.state import NullReservationLocks
from apps.payments.models import PaymentRecord, PaymentStatus
from apps.trips import state_machine
from apps.trips.models import Trip, TripEvent, TripEventType
from apps.trips.services import (
    advance_trip_by_driver,
    cancel_trip_by_rider,
    release_trip_by_driver,
    request_trip,
)
from apps.trips.state_machine import Actor
from conftest import client_for, make_driver, make_rider

from .conftest import DROPOFF, PICKUP, S, advance, assert_audit_chain, new_trip, pending_offer

pytestmark = pytest.mark.django_db(transaction=True)
LOCKS = NullReservationLocks()  # PostgreSQL alone must be enough


def in_threads(fns):
    barrier = threading.Barrier(len(fns))

    def wrap(fn):
        def run():
            try:
                barrier.wait(timeout=10)
                return ("ok", fn())
            except Exception as exc:  # noqa: BLE001 - the outcome is what we assert on
                return ("error", exc)
            finally:
                connection.close()

        return run

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        return [f.result(timeout=60) for f in [pool.submit(wrap(fn)) for fn in fns]]


def outcomes(results):
    return Counter(kind if kind == "ok" else type(value).__name__ for kind, value in results)


def assert_consistent(trip):
    events = assert_audit_chain(trip)
    pending = RideOffer.objects.filter(trip=trip, status=OfferStatus.PENDING).count()
    assert pending <= 1 and (pending == 0 or trip.status == S.SEARCHING)
    job = MatchingJob.objects.filter(trip=trip).first()
    if job and job.status == JobStatus.ACTIVE:
        assert trip.status in (S.REQUESTED, S.SEARCHING)
    payment = PaymentRecord.objects.get(trip=trip)
    if trip.status == S.CANCELLED:
        assert payment.status == PaymentStatus.CANCELLED
    status_outbox = OutboxEvent.objects.filter(key=str(trip.pk), event_type="TripStatusChanged")
    assert status_outbox.count() == len(events) - 1  # one per transition, none duplicated
    return events


# --- creation ------------------------------------------------------------------------------
def test_concurrent_requests_from_one_rider_create_exactly_one_trip():
    rider = make_rider()

    def go():
        return request_trip(rider=rider, pickup=PICKUP, dropoff=DROPOFF, ride_category="ECONOMY")

    results = in_threads([go] * 8)
    counts = outcomes(results)
    assert (
        counts["ok"] == 1 and counts["ok"] + counts["ConflictError"] + counts["IntegrityError"] == 8
    ), counts
    trip = Trip.objects.get(rider=rider)
    assert PaymentRecord.objects.filter(trip=trip).count() == 1
    assert MatchingJob.objects.filter(trip=trip).count() == 1
    assert OutboxEvent.objects.filter(event_type="RideRequested", key=str(trip.pk)).count() == 1
    assert_consistent(trip)


def test_concurrent_retries_with_one_idempotency_key_return_the_same_trip():
    rider = make_rider()
    body = {"pickup": {k: str(v) if k != "address" else v for k, v in PICKUP.items()},
            "dropoff": {k: str(v) if k != "address" else v for k, v in DROPOFF.items()},
            "ride_category": "ECONOMY"}  # fmt: skip

    def go():
        client = client_for(rider)
        response = client.post(reverse("v1:trip-create"), body, format="json",
                               HTTP_IDEMPOTENCY_KEY="retry-key-0001")  # fmt: skip
        return response.status_code, response.json()["id"]

    results = in_threads([go] * 6)
    assert {kind for kind, _ in results} == {"ok"}, results
    assert {value for _, value in results} == {(201, str(Trip.objects.get(rider=rider).pk))}


# --- assignment vs cancellation -------------------------------------------------------------------
@pytest.mark.parametrize("attempt", range(3))
def test_rider_cancel_races_driver_accept(attempt):
    driver = make_driver(online=True)
    trip = advance(new_trip(), S.SEARCHING)
    offer = pending_offer(trip, driver)

    results = in_threads([
        lambda: cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider, reason="race"),
        lambda: accept_offer(offer_id=offer.pk, driver=driver, locks=LOCKS),
    ])  # fmt: skip
    trip.refresh_from_db()
    offer.refresh_from_db()
    events = assert_consistent(trip)
    # The rider may always cancel, so the trip always ends CANCELLED; what differs
    # is whether the driver got it first.
    assert trip.status == S.CANCELLED and results[0][0] == "ok"
    if offer.status == OfferStatus.ACCEPTED:
        assert [e.to_status for e in events][-2:] == [S.DRIVER_ASSIGNED, S.CANCELLED]
    else:
        assert offer.status == OfferStatus.CANCELLED and results[1][0] == "error"
        assert S.DRIVER_ASSIGNED not in [e.to_status for e in events]


def test_offer_expiry_sweeper_races_accept_without_deadlock():
    """Sweeper locks offers (not trips) and writes audit rows; accept locks the trip
    first and then the offer. With a DB foreign key from TripEvent to Trip this
    pairing could deadlock; it must simply produce one consistent outcome."""
    for _ in range(5):
        driver = make_driver(online=True)
        trip = advance(new_trip(), S.SEARCHING)
        offer = pending_offer(trip, driver, ttl=-1)  # already past expiry

        results = in_threads([
            lambda o=offer, d=driver: accept_offer(offer_id=o.pk, driver=d, locks=LOCKS),
            lambda: expire_offers(locks=LOCKS, now=timezone.now() + timedelta(seconds=1)),
        ])  # fmt: skip
        assert not any(isinstance(v, Exception) and "deadlock" in str(v) for _, v in results)
        offer.refresh_from_db()
        trip.refresh_from_db()
        assert offer.status == OfferStatus.EXPIRED and trip.status == S.SEARCHING
        expired = TripEvent.objects.filter(trip_id=trip.pk, event_type=TripEventType.OFFER_EXPIRED)
        assert expired.count() == 1
        assert_consistent(trip)


# --- milestones and release ---------------------------------------------------------------
def test_double_tap_complete_records_one_completion():
    driver = make_driver(online=True)
    trip = advance(new_trip(), S.SEARCHING, S.DRIVER_ASSIGNED, S.DRIVER_ARRIVING, S.IN_PROGRESS,
                   driver=driver)  # fmt: skip

    def go():
        return advance_trip_by_driver(trip_id=trip.pk, driver=driver, milestone="complete")

    results = in_threads([go] * 5)
    assert outcomes(results) == {"ok": 5}  # idempotent, not an error
    trip.refresh_from_db()
    assert trip.status == S.COMPLETED and trip.state_version == 5
    assert OutboxEvent.objects.filter(event_type="TripCompleted", key=str(trip.pk)).count() == 1
    assert_consistent(trip)


def test_driver_release_races_rider_cancel():
    driver = make_driver(online=True)
    trip = advance(new_trip(), S.SEARCHING, S.DRIVER_ASSIGNED, driver=driver)

    in_threads([
        lambda: release_trip_by_driver(trip_id=trip.pk, driver=driver),
        lambda: cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider),
    ])  # fmt: skip
    trip.refresh_from_db()
    assert trip.status == S.CANCELLED  # either order ends cancelled; never lost
    assert_consistent(trip)
    assert not MatchingJob.objects.filter(trip=trip, status=JobStatus.ACTIVE).exists()


def test_unlocked_writers_on_stale_copies_have_exactly_one_winner():
    """Compare-and-set alone (no SELECT FOR UPDATE) still serialises writers."""
    trip = new_trip()
    rider = trip.rider

    def go():
        copy = Trip.objects.get(pk=trip.pk)  # everyone reads v0
        with transaction.atomic():
            return state_machine.transition(copy, S.CANCELLED, actor=Actor.rider(rider))

    results = in_threads([go] * 6)
    counts = outcomes(results)
    assert counts["ok"] == 1 and counts["StaleTripState"] == 5, counts
    trip.refresh_from_db()
    assert trip.state_version == 1
    assert_consistent(trip)
