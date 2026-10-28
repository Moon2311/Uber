"""Matching workflow integration tests (Redis live index + Redis locks + PostgreSQL)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.common.redis import get_redis
from apps.drivers.models import DriverProfile
from apps.drivers.services import set_availability
from apps.events.models import OutboxEvent
from apps.matching.models import JobStatus, MatchingJob, OfferStatus, RideOffer
from apps.matching.offers import ReserveFailure, expire_offers
from apps.matching.orchestrator import RoundOutcome
from apps.matching.state import RedisReservationLocks
from apps.payments.models import PaymentRecord
from apps.trips.models import Trip, TripStatus
from apps.trips.services import cancel_trip_by_rider
from conftest import client_for, make_rider

from .conftest import PICKUP, driver_at, new_trip, north, orchestrator

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("matching_env")]


def lock_holder(driver):
    from django.conf import settings

    return get_redis().get(f"{settings.REDIS_KEY_PREFIX}:{{match}}:resv:{driver.pk}")


def pending(trip):
    return RideOffer.objects.filter(trip=trip, status=OfferStatus.PENDING).first()


def accept(driver, offer):
    return client_for(driver.user).post(f"/api/v1/matching/offers/{offer.id}/accept/")


def decline(driver, offer):
    return client_for(driver.user).post(f"/api/v1/matching/offers/{offer.id}/decline/")


def outbox_types():
    return list(OutboxEvent.objects.order_by("id").values_list("event_type", flat=True))


# --- happy path ----------------------------------------------------------------------------
def test_request_offer_accept(recorded_metrics):
    near, far = driver_at(*north(0.4)), driver_at(*north(1.5))
    trip = new_trip()
    assert MatchingJob.objects.get(trip=trip).status == JobStatus.ACTIVE  # created with trip

    [report] = orchestrator().run_once()
    assert report.outcome is RoundOutcome.OFFERED and report.eligible == 2
    trip.refresh_from_db()
    assert trip.status == TripStatus.SEARCHING
    offer = pending(trip)
    assert offer.driver_id == near.pk and offer.rank == 1
    assert offer.eta_source == "fallback_straight_line" and offer.eta_is_fallback
    assert abs((offer.expires_at - timezone.now()).total_seconds() - 15) < 3
    assert lock_holder(near) == str(trip.pk)  # fast-path reservation held
    assert "RideOfferCreated" in outbox_types()

    listed = client_for(near.user).get("/api/v1/matching/offers/").json()
    assert [o["id"] for o in listed] == [str(offer.id)] and listed[0]["eta_is_estimate"]
    assert client_for(far.user).get("/api/v1/matching/offers/").json() == []

    r = accept(near, offer)
    assert r.status_code == 200 and r.json()["status"] == "ACCEPTED"
    trip.refresh_from_db()
    assert trip.status == TripStatus.DRIVER_ASSIGNED and trip.driver_id == near.pk
    assert MatchingJob.objects.get(trip=trip).status == JobStatus.MATCHED
    assert lock_holder(near) is None
    assert outbox_types()[-2:] == ["TripStatusChanged", "RideAccepted"]

    # metrics required by the step
    assert (
        recorded_metrics.count("matching.offer.created", eta_source="fallback_straight_line") == 1
    )
    assert recorded_metrics.count("matching.offer.accepted") == 1
    names = {k[0] for k in recorded_metrics.timings} | {k[0] for k in recorded_metrics.gauges}
    assert {"matching.round.latency_ms", "matching.eta.latency_ms", "matching.candidates.raw",
            "matching.candidates.eligible", "matching.search.radius_km",
            "matching.search.rings"} <= names  # fmt: skip


def test_accept_is_idempotent_and_final():
    d = driver_at(*north(0.3))
    trip = new_trip()
    orchestrator().run_once()
    offer = pending(trip)
    assert accept(d, offer).status_code == 200
    assert accept(d, offer).status_code == 200  # replay
    assert decline(d, offer).status_code == 409  # cannot change the outcome
    assert Trip.objects.get(pk=trip.pk).driver_id == d.pk


def test_offers_are_private_to_their_driver():
    d, other = driver_at(*north(0.3)), driver_at(*north(9))
    trip = new_trip()
    orchestrator().run_once()
    offer = pending(trip)
    assert accept(other, offer).status_code == 404
    rider_client = client_for(trip.rider)
    assert rider_client.post(f"/api/v1/matching/offers/{offer.id}/accept/").status_code == 403
    assert pending(trip).driver_id == d.pk


# --- next candidate on decline / expiry ------------------------------------------------------
def test_decline_moves_to_next_candidate_and_never_reoffers():
    first, second = driver_at(*north(0.3)), driver_at(*north(0.9))
    trip = new_trip()
    orch = orchestrator()
    orch.run_once()
    assert decline(first, pending(trip)).status_code == 200
    assert lock_holder(first) is None
    [report] = orch.run_once()  # job was woken by the decline
    assert report.outcome is RoundOutcome.OFFERED and pending(trip).driver_id == second.pk
    assert report.rejected.get("excluded") == 1


def test_expiry_moves_to_next_candidate():
    first, second = driver_at(*north(0.3)), driver_at(*north(0.9))
    trip = new_trip()
    orch = orchestrator()
    orch.run_once()
    offer = pending(trip)
    assert orch.run_once() == []  # job sleeps until the offer expires
    RideOffer.objects.filter(pk=offer.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert expire_offers(locks=orch.state.locks) == 1
    offer.refresh_from_db()
    assert offer.status == OfferStatus.EXPIRED and offer.end_reason == "timeout"
    assert lock_holder(first) is None
    orch.run_once()
    assert pending(trip).driver_id == second.pk


def test_accepting_an_expired_offer_fails_and_rematches():
    d, backup = driver_at(*north(0.3)), driver_at(*north(0.9))
    trip = new_trip()
    orch = orchestrator()
    orch.run_once()
    offer = pending(trip)
    RideOffer.objects.filter(pk=offer.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    r = accept(d, offer)
    assert r.status_code == 409 and "expired" in r.json()["error"]["message"]
    offer.refresh_from_db()
    assert offer.status == OfferStatus.EXPIRED
    orch.run_once()
    assert pending(trip).driver_id == backup.pk


# --- search / ranking --------------------------------------------------------------------------
def test_expands_k_ring_when_pickup_area_is_empty():
    far = driver_at(*north(2.5))
    trip = new_trip()
    [report] = orchestrator().run_once()
    assert report.outcome is RoundOutcome.OFFERED and pending(trip).driver_id == far.pk
    assert report.rings_searched >= 3 and report.radius_km == pytest.approx(2.5, rel=0.02)


def test_stops_one_ring_after_target_is_met(settings):
    settings.MATCHING_TARGET_CANDIDATES = 2
    for km in (0.05, 0.1, 0.15):
        driver_at(*north(km))
    driver_at(*north(4))
    new_trip()
    [report] = orchestrator().run_once()
    assert report.rings_searched == 3  # ring 0, ring 1 (target met), ring 2 (boundary)
    assert report.eligible == 3


def test_best_ranked_not_just_nearest():
    driver_at(*north(0.3), rating="3.00")  # nearer, but 2 stars below = +120 s
    good = driver_at(*north(0.6))
    trip = new_trip()
    orchestrator().run_once()
    assert pending(trip).driver_id == good.pk


def test_incompatible_vehicle_is_never_offered():
    driver_at(*north(0.2), category="XL")
    trip = new_trip(category="ECONOMY")
    [report] = orchestrator().run_once()
    assert report.outcome is RoundOutcome.NO_CANDIDATES and report.rejected == {"incompatible": 1}
    assert pending(trip) is None


# --- bounded retries / timeouts ------------------------------------------------------------------
def test_no_candidates_backs_off_then_exhausts_at_deadline():
    trip = new_trip()
    orch = orchestrator()
    [report] = orch.run_once()
    assert report.outcome is RoundOutcome.NO_CANDIDATES
    job = MatchingJob.objects.get(trip=trip)
    assert job.rounds == 1 and job.next_run_at > timezone.now() and job.lease_owner == ""
    assert orch.run_once() == []  # backing off
    MatchingJob.objects.filter(trip=trip).update(
        next_run_at=timezone.now(), deadline=timezone.now() - timedelta(seconds=1)
    )
    [report] = orch.run_once()
    assert report.outcome is RoundOutcome.EXHAUSTED
    trip.refresh_from_db()
    assert trip.status == TripStatus.CANCELLED
    assert trip.cancellation_reason == "no_driver_available"
    assert MatchingJob.objects.get(trip=trip).status == JobStatus.EXHAUSTED
    assert PaymentRecord.objects.get(trip=trip).status == "CANCELLED"


def test_max_offers_per_trip(settings):
    settings.MATCHING_MAX_OFFERS_PER_TRIP = 2
    drivers = [driver_at(*north(0.2 * (i + 1))) for i in range(3)]
    trip = new_trip()
    orch = orchestrator()
    for d in drivers[:2]:
        orch.run_once()
        decline(d, pending(trip))
    [report] = orch.run_once()
    assert report.outcome is RoundOutcome.EXHAUSTED
    assert not RideOffer.objects.filter(driver=drivers[2]).exists()


# --- reservation failures release safely ---------------------------------------------------
def test_failed_reservation_releases_lock_and_tries_next(monkeypatch):
    first, second = driver_at(*north(0.2)), driver_at(*north(0.8))
    trip = new_trip()
    orch = orchestrator()
    original = orch.state.filter_eligible

    def then_first_goes_busy(*args, **kwargs):
        result = original(*args, **kwargs)
        if not Trip.objects.filter(driver=first).exists():
            # between eligibility check and reservation, `first` gets another trip
            other = new_trip()
            Trip.objects.filter(pk=other.pk).update(driver=first, status=TripStatus.DRIVER_ASSIGNED)
        return result

    monkeypatch.setattr(orch.state, "filter_eligible", then_first_goes_busy)
    [report] = orch.run_once()
    assert report.attempts == 2 and pending(trip).driver_id == second.pk
    assert lock_holder(first) is None  # released after the failed attempt


def test_lock_held_elsewhere_is_skipped():
    first, second = driver_at(*north(0.2)), driver_at(*north(0.8))
    RedisReservationLocks().try_acquire(first.pk, "some-other-trip", 30)
    trip = new_trip()
    [report] = orchestrator().run_once()
    assert pending(trip).driver_id == second.pk and report.rejected.get("reserved") == 1
    assert lock_holder(first) == "some-other-trip"  # someone else's lock is untouched


def test_works_without_redis_locks():
    d = driver_at(*north(0.2))
    trip = new_trip()
    orchestrator(locks="none").run_once()
    assert pending(trip).driver_id == d.pk and lock_holder(d) is None


def test_redis_lock_outage_falls_back_to_postgres(monkeypatch, recorded_metrics):
    import redis

    d = driver_at(*north(0.2))
    trip = new_trip()
    orch = orchestrator()

    def down(*a, **k):
        raise redis.exceptions.ConnectionError("lock redis down")

    monkeypatch.setattr(orch.state.locks, "_acquire", down)
    monkeypatch.setattr(orch.state.locks.r, "mget", down)
    orch.run_once()
    assert pending(trip).driver_id == d.pk
    assert recorded_metrics.count("matching.reservation_lock.unavailable") >= 1


def test_lease_fencing_prevents_stale_worker_from_offering():
    driver_at(*north(0.2))
    trip = new_trip()
    stale = orchestrator()
    assert stale.claim(10) == [str(trip.pk)]
    MatchingJob.objects.filter(trip=trip).update(lease_owner="someone-else")  # lease taken over
    report = stale.process(str(trip.pk))
    assert report.outcome is RoundOutcome.LEASE_LOST and pending(trip) is None


def test_reserve_reports_lease_lost():
    from apps.matching.offers import reserve_and_offer

    d = driver_at(*north(0.2))
    trip = new_trip()
    orch = orchestrator()
    orch.claim(10)
    orch._start_searching(str(trip.pk))
    ranked = orch._candidates(trip, type("R", (), {"rings_searched": 0, "raw_candidates": 0,
                                                   "radius_km": 0.0})())  # fmt: skip
    MatchingJob.objects.filter(trip=trip).update(lease_until=timezone.now() - timedelta(1))
    result = reserve_and_offer(
        trip_id=trip.pk, candidate=ranked[0], lease_owner=orch.owner, locks=orch.state.locks,
        compatible_categories={"ECONOMY"},
    )  # fmt: skip
    assert result.failure is ReserveFailure.LEASE_LOST and lock_holder(d) is None


# --- interaction with other domain events ------------------------------------------------------
def test_driver_going_offline_cancels_offer_and_rematches():
    first, second = driver_at(*north(0.2)), driver_at(*north(0.8))
    trip = new_trip()
    orch = orchestrator()
    orch.run_once()
    offer = pending(trip)
    set_availability(driver=DriverProfile.objects.get(pk=first.pk), online=False)
    offer.refresh_from_db()
    assert offer.status == OfferStatus.CANCELLED and offer.end_reason == "driver_offline"
    assert lock_holder(first) is None
    orch.run_once()
    assert pending(trip).driver_id == second.pk


def test_rider_cancelling_stops_matching_and_releases_driver():
    d = driver_at(*north(0.2))
    trip = new_trip()
    orch = orchestrator()
    orch.run_once()
    offer = pending(trip)
    cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider)
    offer.refresh_from_db()
    assert offer.status == OfferStatus.CANCELLED and lock_holder(d) is None
    assert MatchingJob.objects.get(trip=trip).status == JobStatus.CANCELLED
    assert accept(d, offer).status_code == 409
    assert orch.run_once() == []


# --- idempotency keys on ride requests -----------------------------------------------------------
TRIP_BODY = {
    "pickup": {"lat": PICKUP[0], "lng": PICKUP[1]},
    "dropoff": {"lat": 37.8044, "lng": -122.2712},
    "ride_category": "ECONOMY",
}


def test_idempotency_key_makes_ride_requests_retry_safe():
    client = client_for(make_rider())
    first = client.post(
        "/api/v1/trips/", TRIP_BODY, format="json", HTTP_IDEMPOTENCY_KEY="req-12345678"
    )
    retry = client.post(
        "/api/v1/trips/", TRIP_BODY, format="json", HTTP_IDEMPOTENCY_KEY="req-12345678"
    )
    assert first.status_code == retry.status_code == 201
    assert first.json()["id"] == retry.json()["id"] and retry["Idempotent-Replayed"] == "true"
    assert Trip.objects.count() == 1 and MatchingJob.objects.count() == 1


def test_idempotency_key_reuse_with_different_body_is_rejected():
    client = client_for(make_rider())
    client.post("/api/v1/trips/", TRIP_BODY, format="json", HTTP_IDEMPOTENCY_KEY="req-abcdefgh")
    other = {**TRIP_BODY, "ride_category": "XL"}
    r = client.post("/api/v1/trips/", other, format="json", HTTP_IDEMPOTENCY_KEY="req-abcdefgh")
    assert r.status_code == 422 and r.json()["error"]["code"] == "idempotency_key_reused"
    bad = client.post("/api/v1/trips/", TRIP_BODY, format="json", HTTP_IDEMPOTENCY_KEY="x")
    assert bad.status_code == 400
