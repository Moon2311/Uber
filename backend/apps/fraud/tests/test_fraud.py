"""Fraud: signals from Kafka events, scoring, and the difference between
asynchronous monitoring (cases for humans) and blocking checks."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone as djtz

from apps.events import builders
from apps.events.consumer import ConsumedMessage, EventProcessor, Outcome
from apps.events.schemas import make_event
from apps.events.tests.fakes import FakePublisher
from apps.fraud import scoring
from apps.fraud.guards import ActionBlocked, check
from apps.fraud.handlers import HANDLERS
from apps.fraud.models import (
    CaseStatus,
    Decision,
    FraudCase,
    FraudDecision,
    RiskProfile,
    RiskSignal,
    Subject,
)
from apps.trips.models import Trip, TripStatus
from apps.trips.tests.conftest import new_trip
from conftest import client_for, make_driver, make_rider

pytestmark = pytest.mark.django_db
T0 = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def consume(*events):
    processor = EventProcessor("fraud-test", HANDLERS, FakePublisher(), sleep=lambda s: None)
    return [processor.process(ConsumedMessage("t", 0, i, "k", e.to_bytes()))
            for i, e in enumerate(events)]  # fmt: skip


def gps(driver_id, lat, lng, at):
    return make_event("DriverLocationUpdated", {
        "driver_id": driver_id, "lat": Decimal(str(lat)), "lng": Decimal(str(lng)),
        "recorded_at": at, "h3_cell": None, "h3_resolution": None, "reason": "interval",
    })  # fmt: skip


def payment_failed(rider_id, code="card_declined", at=None):
    return make_event("PaymentStatusChanged", {
        "payment_id": uuid.uuid4(), "trip_id": uuid.uuid4(), "rider_id": rider_id,
        "from_status": "PROCESSING", "to_status": "FAILED", "amount": Decimal("10.00"),
        "currency": "USD", "method": "CARD", "failure_code": code,
        "changed_at": at or djtz.now(),
    })  # fmt: skip


def signal_for(rider, signal, n=1):
    for _ in range(n):
        scoring.record_signal(subject_type=Subject.RIDER, subject_id=rider.pk, signal=signal,
                              source_event_id=uuid.uuid4(), observed_at=djtz.now())  # fmt: skip
    scoring.refresh_profile(Subject.RIDER, rider.pk)


# --- GPS: unreliable, never blocks ----------------------------------------------------------------
def test_one_impossible_gps_jump_is_a_low_reliability_signal_only():
    driver = make_driver(online=True)
    consume(
        gps(driver.pk, 37.77, -122.42, T0),
        gps(driver.pk, 34.05, -118.24, T0 + timedelta(minutes=2)),
    )  # SF -> LA in 2 min
    [sig] = RiskSignal.objects.filter(subject_type=Subject.DRIVER, subject_id=driver.pk)
    assert sig.signal == "gps_impossible_speed" and sig.reliability == "LOW"
    assert sig.evidence["kmh"] > 10_000
    profile = RiskProfile.objects.get(subject_type=Subject.DRIVER, subject_id=driver.pk)
    assert profile.blocking_score == 0 and profile.level == "LOW"


def test_many_gps_anomalies_cannot_block_anyone():
    rider = make_rider()
    signal_for(rider, "gps_impossible_speed", n=50)
    score = scoring.compute(Subject.RIDER, rider.pk)
    assert score.score == 30 and score.blocking_score == 0  # capped, excluded from blocking
    assert scoring.decide(Subject.RIDER, rider.pk).decision == Decision.ALLOW
    new_trip(rider)  # not blocked


def test_normal_driving_and_out_of_order_samples_raise_nothing():
    driver = make_driver(online=True)
    consume(
        gps(driver.pk, 37.7700, -122.4200, T0),
        gps(driver.pk, 37.7790, -122.4200, T0 + timedelta(minutes=1)),  # ~1 km/min = 60 km/h
        gps(driver.pk, 34.05, -118.24, T0 - timedelta(minutes=5)),
    )  # stale, delayed sample
    assert not RiskSignal.objects.exists()


# --- reliable signals and corroboration -----------------------------------------------------------
def test_payment_failures_are_high_reliability_and_ignore_our_own_outages():
    rider = make_rider()
    consume(
        payment_failed(rider.pk, "provider_unavailable"),
        payment_failed(rider.pk, "no_payment_method"),
    )
    assert not RiskSignal.objects.exists()
    consume(payment_failed(rider.pk))
    assert RiskSignal.objects.get().reliability == "HIGH"


def test_elevated_score_opens_a_review_case_but_does_not_block():
    rider = make_rider()
    signal_for(rider, "repeated_payment_failures", n=5)  # 150 points, capped at 30 per type
    signal_for(rider, "payment_failed", n=1)  # +15 = 45: above review (40), below block (70)
    assessment = scoring.decide(Subject.RIDER, rider.pk)
    assert assessment.decision == Decision.REVIEW
    assert FraudCase.objects.get(subject_id=rider.pk).status == CaseStatus.OPEN  # human looks
    check(action="trip_request", user=rider)  # allowed
    assert FraudDecision.objects.get().decision == Decision.REVIEW


def test_corroborated_reliable_signals_block_sensitive_actions():
    rider = make_rider()
    signal_for(rider, "repeated_payment_failures", n=1)  # 30 HIGH
    signal_for(rider, "payment_failed", n=2)  # 30 HIGH
    signal_for(rider, "rider_rapid_cancellations", n=1)  # 15 MEDIUM -> 75, 3 types
    response = client_for(rider).post(reverse("v1:trip-create"), {
        "pickup": {"lat": "37.7749", "lng": "-122.4194"},
        "dropoff": {"lat": "37.8044", "lng": "-122.2712"}, "ride_category": "ECONOMY",
    }, format="json")  # fmt: skip
    assert response.status_code == 403
    body = response.json()["error"]
    assert body["code"] == "action_blocked" and "score" not in str(body)  # no scoring leak
    decision = FraudDecision.objects.get()
    assert decision.decision == Decision.BLOCK and decision.blocking_score >= 70
    assert not Trip.objects.filter(rider=rider).exists()


def test_manual_hold_blocks_and_dismissal_lifts_it():
    rider = make_rider()
    RiskProfile.objects.create(subject_type=Subject.RIDER, subject_id=rider.pk, hold=True)
    with pytest.raises(ActionBlocked):
        check(action="trip_request", user=rider)
    RiskProfile.objects.filter(subject_id=rider.pk).update(hold=False)
    check(action="trip_request", user=rider)


def test_signals_expire():
    rider = make_rider()
    signal_for(rider, "repeated_payment_failures")
    signal_for(rider, "payment_failed", n=2)
    signal_for(rider, "rider_rapid_cancellations")
    assert scoring.decide(Subject.RIDER, rider.pk).decision == Decision.BLOCK
    later = djtz.now() + timedelta(days=4)  # past every ttl_hours above
    assert scoring.decide(Subject.RIDER, rider.pk, now=later).decision == Decision.ALLOW


# --- trip-derived signals (authoritative PostgreSQL lookups) --------------------------------------
def test_rapid_rider_cancellations(settings):
    settings.FRAUD_RULES = {**settings.FRAUD_RULES, "rider_rapid_cancellations": {
        **settings.FRAUD_RULES["rider_rapid_cancellations"], "threshold": 3}}  # fmt: skip
    from apps.trips.services import cancel_trip_by_rider

    rider = make_rider()
    events = []
    for _ in range(3):
        trip = new_trip(rider)
        cancel_trip_by_rider(trip_id=trip.pk, rider=rider)
        trip.refresh_from_db()
        events.append(builders.trip_status_changed(trip, "REQUESTED", "CANCELLED"))
    consume(*events)
    [sig] = RiskSignal.objects.filter(signal="rider_rapid_cancellations")
    assert sig.subject_id == rider.pk and sig.evidence["cancellations"] == 3


def test_system_cancellations_are_not_held_against_the_rider():
    rider = make_rider()
    trip = new_trip(rider)
    Trip.objects.filter(pk=trip.pk).update(status=TripStatus.CANCELLED, cancelled_by="SYSTEM",
                                           cancelled_at=djtz.now())  # fmt: skip
    trip.refresh_from_db()
    consume(builders.trip_status_changed(trip, "SEARCHING", "CANCELLED"))
    assert not RiskSignal.objects.exists()


def test_implausibly_fast_trip_flags_the_driver():
    driver = make_driver(online=True)
    event = make_event("TripCompleted", {
        "trip_id": uuid.uuid4(), "rider_id": make_rider().pk, "driver_id": driver.pk,
        "ride_category": "ECONOMY", "estimated_fare": Decimal("30"), "final_fare": Decimal("30"),
        "currency": "USD", "estimated_distance_km": Decimal("25"), "started_at": T0,
        "completed_at": T0 + timedelta(minutes=3),  # 25 km in 3 min = 500 km/h
    })  # fmt: skip
    consume(event)
    [sig] = RiskSignal.objects.filter(subject_type=Subject.DRIVER)
    assert sig.signal == "implausible_trip_speed" and sig.reliability == "MEDIUM"


# --- idempotency and failure behaviour ------------------------------------------------------------
def test_redelivered_events_do_not_add_risk_twice():
    rider = make_rider()
    event = payment_failed(rider.pk)
    assert consume(event, event) == [Outcome.HANDLED, Outcome.DUPLICATE]
    assert RiskSignal.objects.count() == 1


def test_check_failure_fails_open_for_trips_and_closed_for_payment_methods(monkeypatch):
    rider = make_rider()

    def broken(*a, **kw):
        raise RuntimeError("scoring bug")

    monkeypatch.setattr("apps.fraud.guards.decide", broken)
    check(action="trip_request", user=rider)  # allowed: availability first
    response = client_for(rider).post(
        reverse("v1:payment-methods"), {"provider_token": "tok_sandbox_visa"}, format="json"
    )
    assert response.status_code == 503  # refused: card testing target


def test_card_testing_velocity_is_a_hard_limit(settings):
    rider = make_rider()
    client = client_for(rider)
    url = reverse("v1:payment-methods")
    codes = [client.post(url, {"provider_token": "tok_sandbox_visa"}, format="json").status_code
             for _ in range(settings.FRAUD_RULES["card_testing"]["threshold"] + 1)]  # fmt: skip
    assert codes[:-1] == [201] * settings.FRAUD_RULES["card_testing"]["threshold"]
    assert codes[-1] == 403
    assert FraudDecision.objects.filter(decision=Decision.BLOCK,
                                        reasons=["card_testing_velocity"]).exists()  # fmt: skip
