"""Consumer: idempotency, retries, dead-lettering, rollback, infra errors."""

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from django.db import OperationalError

from apps.analytics.handlers import HANDLERS
from apps.analytics.models import DriverDailyStats, TripTimelineEntry
from apps.events import builders
from apps.events.consumer import (
    ConsumedMessage,
    EventProcessor,
    InfrastructureError,
    Outcome,
)
from apps.events.models import ProcessedEvent

from .fakes import FakePublisher

pytestmark = pytest.mark.django_db
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def completed_event(driver_id=7, fare="10.00"):
    return builders.make_event(
        "TripCompleted",
        {"trip_id": uuid.uuid4(), "rider_id": 1, "driver_id": driver_id,
         "ride_category": "ECONOMY", "estimated_fare": Decimal(fare), "final_fare": None,
         "currency": "USD", "estimated_distance_km": Decimal("3"), "started_at": NOW,
         "completed_at": NOW},
    )  # fmt: skip


def msg(event_or_bytes, offset=0, topic="trip.events.v1"):
    value = event_or_bytes if isinstance(event_or_bytes, bytes) else event_or_bytes.to_bytes()
    return ConsumedMessage(topic, 0, offset, "k", value, [("event_type", b"x")])


@pytest.fixture
def dlq():
    return FakePublisher()


@pytest.fixture
def sleeps():
    return []


def processor(dlq, sleeps, handlers=None, **kw):
    return EventProcessor("analytics", handlers or HANDLERS, dlq, sleep=sleeps.append, **kw)


def stats():
    return DriverDailyStats.objects.get(driver_id=7)


def test_handles_once_and_records_ledger(dlq, sleeps):
    event = completed_event()
    assert processor(dlq, sleeps).process(msg(event)) is Outcome.HANDLED
    assert stats().completed_trips == 1 and stats().gross_estimated_fare == Decimal("10.00")
    assert ProcessedEvent.objects.filter(event_id=event.event_id, consumer_group="analytics")


def test_duplicate_delivery_is_not_double_counted(dlq, sleeps, recorded_metrics):
    event = completed_event()
    p = processor(dlq, sleeps)
    assert p.process(msg(event, offset=0)) is Outcome.HANDLED
    assert p.process(msg(event, offset=0)) is Outcome.DUPLICATE  # same offset redelivered
    assert p.process(msg(event, offset=99)) is Outcome.DUPLICATE  # republished by relay
    assert stats().completed_trips == 1
    assert recorded_metrics.count("events.consumer.duplicate", group="analytics") == 2


def test_groups_are_independent(dlq, sleeps):
    event = completed_event()
    processor(dlq, sleeps).process(msg(event))
    other = EventProcessor("billing-audit", {"TripCompleted": lambda e: None}, dlq)
    assert other.process(msg(event)) is Outcome.HANDLED


def test_transient_error_is_retried_with_backoff(dlq, sleeps, settings):
    settings.CONSUMER_RETRY_BASE_SECONDS = 0.5
    calls = {"n": 0}

    def flaky(event):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("downstream hiccup")
        HANDLERS["TripCompleted"](event)

    outcome = processor(dlq, sleeps, {"TripCompleted": flaky}).process(msg(completed_event()))
    assert outcome is Outcome.HANDLED and calls["n"] == 3
    assert sleeps == [0.5, 1.0] and dlq.sent == []
    assert stats().completed_trips == 1


def test_persistent_failure_goes_to_dlq_after_retries(dlq, sleeps):
    def broken(event):
        raise ValueError("bug")

    event = completed_event()
    outcome = processor(dlq, sleeps, {"TripCompleted": broken}, max_retries=3).process(
        msg(event, offset=42)
    )
    assert outcome is Outcome.DEAD_LETTERED and len(sleeps) == 3
    [dead] = dlq.sent
    h = dict(dead.headers)
    assert dead.topic == "events.dlq.v1" and dead.value == event.to_bytes()
    assert h["dlq_original_topic"] == b"trip.events.v1" and h["dlq_original_offset"] == b"42"
    assert h["dlq_error_class"] == b"ValueError" and h["dlq_attempts"] == b"4"
    assert h["dlq_consumer_group"] == b"analytics" and h["event_type"] == b"x"  # original kept
    assert not ProcessedEvent.objects.exists()  # not marked processed


def test_permanent_error_dead_letters_immediately(dlq, sleeps):
    outcome = processor(dlq, sleeps).process(msg(completed_event(fare="-1.00")))
    assert outcome is Outcome.DEAD_LETTERED and sleeps == []
    assert dict(dlq.sent[0].headers)["dlq_error_class"] == b"PermanentError"
    assert not DriverDailyStats.objects.filter(completed_trips__gt=0).exists()


@pytest.mark.parametrize(
    "raw",
    [b"garbage", json.dumps({"event_id": str(uuid.uuid4()), "event_type": "TripCompleted",
                              "event_version": 7, "occurred_at": "x", "payload": {}}).encode()],
)  # fmt: skip
def test_undecodable_or_unknown_version_dead_letters(dlq, sleeps, raw):
    assert processor(dlq, sleeps).process(msg(raw)) is Outcome.DEAD_LETTERED
    assert dict(dlq.sent[0].headers)["dlq_attempts"] == b"0"


def test_unhandled_event_type_is_ignored(dlq, sleeps):
    e = builders.ride_accepted(trip_id=uuid.uuid4(), offer_id=uuid.uuid4(), driver_id=1,
                               accepted_at=NOW)  # fmt: skip
    assert processor(dlq, sleeps).process(msg(e)) is Outcome.IGNORED


def test_failed_attempt_rolls_back_partial_side_effects(dlq, sleeps):
    def half_done(event):
        HANDLERS["TripCompleted"](event)
        raise ValueError("after the write")

    processor(dlq, sleeps, {"TripCompleted": half_done}, max_retries=1).process(
        msg(completed_event())
    )
    assert not DriverDailyStats.objects.filter(completed_trips__gt=0).exists()


def test_database_outage_is_not_dead_lettered(dlq, sleeps):
    def db_down(event):
        raise OperationalError("connection refused")

    with pytest.raises(InfrastructureError):
        processor(dlq, sleeps, {"TripCompleted": db_down}).process(msg(completed_event()))
    assert dlq.sent == [] and sleeps == []


def test_dlq_unavailable_blocks_instead_of_losing_message(sleeps):
    with pytest.raises(InfrastructureError):
        processor(FakePublisher(fail_from=0), sleeps).process(msg(b"garbage"))


def test_timeline_projection(dlq, sleeps):
    trip_id = uuid.uuid4()
    p = processor(dlq, sleeps)
    for frm, to in [(None, "REQUESTED"), ("REQUESTED", "CANCELLED")]:
        p.process(msg(builders.make_event("TripStatusChanged", {
            "trip_id": trip_id, "from_status": frm, "to_status": to, "rider_id": 1,
            "driver_id": None, "changed_at": NOW})))  # fmt: skip
    timeline = TripTimelineEntry.objects.filter(trip_id=trip_id)
    assert list(timeline.values_list("status", flat=True)) == ["REQUESTED", "CANCELLED"]
