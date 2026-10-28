"""Concurrency: competing requests, workers and responses, with real threads,
transactions (transaction=True) and Redis. Each scenario runs with the Redis
reservation locks AND with PostgreSQL alone, proving correctness never depends on
Redis."""

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.drivers.models import DriverProfile
from apps.matching.models import MatchingJob, OfferStatus, RideOffer
from apps.matching.offers import accept_offer, expire_offers
from apps.matching.state import NullReservationLocks
from apps.trips.models import DRIVER_ACTIVE_STATUSES, Trip, TripStatus

from .conftest import driver_at, new_trip, north, orchestrator

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("matching_env")]
LOCKS = pytest.mark.parametrize("locks", ["redis", "none"])


def in_threads(fns):
    """Run callables simultaneously (barrier), each with its own DB connection."""
    barrier = threading.Barrier(len(fns))

    def wrap(fn):
        def run():
            try:
                barrier.wait(timeout=10)
                return fn()
            finally:
                connection.close()

        return run

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        futures = [pool.submit(wrap(fn)) for fn in fns]
        return [f.result(timeout=60) for f in futures]


def assert_invariants():
    pending_by_driver = Counter(
        RideOffer.objects.filter(status=OfferStatus.PENDING).values_list("driver_id", flat=True)
    )
    assert all(n == 1 for n in pending_by_driver.values()), pending_by_driver
    pending_by_trip = Counter(
        RideOffer.objects.filter(status=OfferStatus.PENDING).values_list("trip_id", flat=True)
    )
    assert all(n == 1 for n in pending_by_trip.values()), pending_by_trip
    active = Counter(
        Trip.objects.filter(status__in=DRIVER_ACTIVE_STATUSES).values_list("driver_id", flat=True)
    )
    assert all(n == 1 for n in active.values()), active


@LOCKS
def test_two_requests_compete_for_one_driver(locks):
    driver = driver_at(*north(0.3))
    trip_a, trip_b = new_trip(), new_trip()
    workers = [orchestrator(locks), orchestrator(locks)]
    # Each worker claims a different trip, then both race to reserve the same driver.
    claimed = [workers[0].claim(1), workers[1].claim(1)]
    assert sorted(sum(claimed, [])) == sorted([str(trip_a.pk), str(trip_b.pk)])
    reports = in_threads(
        [lambda w=w, c=c: w.process(c[0]) for w, c in zip(workers, claimed, strict=True)]
    )
    outcomes = sorted(r.outcome.value for r in reports)
    assert outcomes == ["offered", "reservation_failed"]
    assert RideOffer.objects.filter(driver=driver).count() == 1
    assert_invariants()


@LOCKS
def test_many_requests_few_drivers_many_workers(locks, settings):
    settings.MATCHING_MAX_RESERVE_ATTEMPTS = 3
    drivers = [driver_at(*north(0.2 * (i + 1))) for i in range(3)]
    trips = [new_trip() for _ in range(6)]
    workers = [orchestrator(locks) for _ in range(3)]
    in_threads([lambda w=w: w.run_once(limit=2) for w in workers])
    assert_invariants()
    offers = RideOffer.objects.filter(status=OfferStatus.PENDING)
    assert offers.count() == 3  # every driver reserved exactly once
    assert {o.driver_id for o in offers} == {d.pk for d in drivers}
    assert len({o.trip_id for o in offers}) == 3
    assert len(trips) == 6


def test_concurrent_workers_never_process_the_same_job():
    driver_at(*north(0.3))
    trip = new_trip()
    workers = [orchestrator() for _ in range(4)]
    claims = in_threads([lambda w=w: w.claim(10) for w in workers])
    assert sorted(c for claim in claims for c in claim) == [str(trip.pk)]  # SKIP LOCKED


def test_double_tap_accept_assigns_once():
    driver = driver_at(*north(0.3))
    trip = new_trip()
    orchestrator().run_once()
    offer = RideOffer.objects.get(trip=trip)
    profile = DriverProfile.objects.get(pk=driver.pk)
    results = in_threads([
        lambda: accept_offer(offer_id=offer.id, driver=profile, locks=NullReservationLocks())
        for _ in range(4)
    ])  # fmt: skip
    assert sorted(r.changed for r in results) == [False, False, False, True]
    trip.refresh_from_db()
    assert trip.status == TripStatus.DRIVER_ASSIGNED and trip.driver_id == driver.pk
    assert Trip.objects.filter(driver=driver).count() == 1
    assert_invariants()


def test_accept_racing_expiry_has_one_consistent_outcome():
    from apps.common.exceptions import ConflictError

    for _ in range(5):  # several rounds to exercise both interleavings
        # Previous rounds' drivers leave the market so each round is independent.
        DriverProfile.objects.update(availability_status="OFFLINE")
        driver = driver_at(*north(0.3))
        trip = new_trip()
        orchestrator().run_once()
        offer = RideOffer.objects.get(trip=trip)
        RideOffer.objects.filter(pk=offer.pk).update(
            expires_at=timezone.now() + timedelta(milliseconds=50)
        )
        profile = DriverProfile.objects.get(pk=driver.pk)

        def do_accept(offer=offer, profile=profile):
            try:
                return accept_offer(offer_id=offer.id, driver=profile, locks=NullReservationLocks())
            except ConflictError:
                return "conflict"

        def do_expire():
            return expire_offers(locks=NullReservationLocks(),
                                 now=timezone.now() + timedelta(seconds=1))  # fmt: skip

        in_threads([do_accept, do_expire])
        offer.refresh_from_db()
        trip.refresh_from_db()
        if offer.status == OfferStatus.ACCEPTED:
            assert trip.driver_id == driver.pk and trip.status == TripStatus.DRIVER_ASSIGNED
        else:
            assert offer.status == OfferStatus.EXPIRED
            assert trip.driver_id is None and trip.status == TripStatus.SEARCHING
        assert_invariants()


def test_rider_cancel_racing_driver_accept_is_consistent():
    from apps.common.exceptions import ConflictError
    from apps.trips.services import cancel_trip_by_rider

    driver = driver_at(*north(0.3))
    trip = new_trip()
    orchestrator().run_once()
    offer = RideOffer.objects.get(trip=trip)
    profile = DriverProfile.objects.get(pk=driver.pk)

    def do_accept():
        try:
            return accept_offer(offer_id=offer.id, driver=profile, locks=NullReservationLocks())
        except ConflictError:
            return "conflict"

    in_threads([do_accept, lambda: cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider)])
    trip.refresh_from_db()
    offer.refresh_from_db()
    assert trip.status == TripStatus.CANCELLED  # rider may cancel before or after assignment
    assert offer.status in (OfferStatus.ACCEPTED, OfferStatus.CANCELLED)
    assert MatchingJob.objects.get(trip=trip).status in ("CANCELLED", "MATCHED")
    assert_invariants()


def test_database_constraints_are_the_last_line_of_defence():
    driver = driver_at(*north(0.3))
    a, b = new_trip(), new_trip()
    common = {"driver": driver, "expires_at": timezone.now() + timedelta(seconds=15), "rank": 1,
              "score": 1.0, "pickup_eta_seconds": 60, "eta_source": "x",
              "eta_is_fallback": True, "straight_line_km": 0.3}  # fmt: skip
    RideOffer.objects.create(trip=a, **common)
    with pytest.raises(IntegrityError), transaction.atomic():
        RideOffer.objects.create(trip=b, **common)  # driver already reserved
    Trip.objects.filter(pk=a.pk).update(driver=driver, status=TripStatus.DRIVER_ASSIGNED)
    with pytest.raises(IntegrityError), transaction.atomic():
        Trip.objects.filter(pk=b.pk).update(driver=driver, status=TripStatus.DRIVER_ASSIGNED)
