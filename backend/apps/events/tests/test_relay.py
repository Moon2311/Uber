"""Outbox relay: ordering, retries/backoff, recovery, give-up, single leader."""

from datetime import timedelta

import psycopg
import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.events import builders, outbox
from apps.events.models import OutboxEvent, OutboxStatus
from apps.events.relay import RELAY_LOCK_ID, OutboxRelay, backoff_seconds, outbox_stats
from apps.events.schemas import decode

from .fakes import FakePublisher

pytestmark = pytest.mark.django_db


def enqueue(n, driver_id=1):
    with transaction.atomic():
        return [
            outbox.enqueue(
                builders.driver_availability_changed(
                    driver_id=driver_id,
                    status="ONLINE" if i % 2 == 0 else "OFFLINE",
                    previous_status=None,
                    reason=f"r{i}",
                    vehicle_category=None,
                    changed_at=timezone.now(),
                )  # fmt: skip
            )
            for i in range(n)
        ]


class Clock:
    def __init__(self):
        self.now = timezone.now()

    def __call__(self):
        return self.now


def statuses():
    return list(OutboxEvent.objects.order_by("id").values_list("status", flat=True))


def test_publishes_in_order_and_marks_rows(settings):
    settings.KAFKA_TOPIC_PREFIX = "env1."
    rows = enqueue(3)
    pub = FakePublisher()
    result = OutboxRelay(pub).run_once()
    assert result.published == 3 and statuses() == ["PUBLISHED"] * 3
    assert [decode(m.value).event_id for m in pub.sent] == [str(r.event_id) for r in rows]
    m = pub.sent[0]
    assert m.topic == "env1.driver.availability.v1" and m.key == "1"
    assert dict(m.headers)["event_type"] == b"DriverAvailabilityChanged"
    assert OutboxRelay(pub).run_once().published == 0  # nothing left


def test_failure_mid_batch_keeps_order_and_backs_off():
    enqueue(4)
    clock = Clock()
    result = OutboxRelay(FakePublisher(fail_from=2), clock=clock).run_once()
    assert result.published == 2 and result.failed == 1
    assert statuses() == ["PUBLISHED", "PUBLISHED", "PENDING", "PENDING"]
    third = OutboxEvent.objects.order_by("id")[2]
    assert third.attempts == 1 and third.last_error == "broker down"
    assert third.next_attempt_at == clock.now + timedelta(seconds=backoff_seconds(1))
    # before the backoff expires nothing is published, not even the 4th row
    healthy = FakePublisher()
    assert OutboxRelay(healthy, clock=clock).run_once().backing_off
    assert healthy.sent == []
    clock.now += timedelta(seconds=backoff_seconds(1))
    assert OutboxRelay(healthy, clock=clock).run_once().published == 2
    assert [decode(m.value).payload["reason"] for m in healthy.sent] == ["r2", "r3"]


def test_backoff_grows_and_is_capped(settings):
    settings.OUTBOX_RETRY_BASE_SECONDS = 1
    settings.OUTBOX_RETRY_MAX_SECONDS = 60
    assert [backoff_seconds(a) for a in (1, 2, 3, 4, 10)] == [1, 2, 4, 8, 60]


def test_recovery_after_long_kafka_outage(settings):
    settings.OUTBOX_MAX_ATTEMPTS = 100
    enqueue(5)
    clock = Clock()
    down = FakePublisher(fail_from=0)
    for _ in range(6):
        OutboxRelay(down, clock=clock).run_once()
        clock.now += timedelta(minutes=5)
    assert statuses() == ["PENDING"] * 5
    assert OutboxEvent.objects.order_by("id").first().attempts == 6
    stats = outbox_stats(now=clock.now)
    assert stats["pending"] == 5 and stats["oldest_pending_age_s"] > 1500
    up = FakePublisher()
    assert OutboxRelay(up, clock=clock).run_once().published == 5
    assert [decode(m.value).payload["reason"] for m in up.sent] == [f"r{i}" for i in range(5)]


def test_crash_after_publish_republishes(settings):
    """At-least-once: if the relay dies after Kafka acked but before marking the
    rows, the next run publishes the same event_ids again (consumers dedupe)."""
    enqueue(2)
    first = FakePublisher()
    OutboxRelay(first).run_once()
    OutboxEvent.objects.update(status=OutboxStatus.PENDING, published_at=None)  # simulate crash
    second = FakePublisher()
    OutboxRelay(second).run_once()
    assert [m.value for m in first.sent] == [m.value for m in second.sent]


def test_poison_event_gives_up_and_unblocks_stream(settings):
    settings.OUTBOX_MAX_ATTEMPTS = 2
    enqueue(2)
    clock = Clock()

    class PoisonFirst(FakePublisher):
        def publish(self, messages, *, timeout):
            return ["MSG_SIZE_TOO_LARGE"] + [None] * (len(messages) - 1)

    for _ in range(2):
        OutboxRelay(PoisonFirst(), clock=clock).run_once()
        clock.now += timedelta(minutes=5)
    assert statuses() == ["FAILED", "PENDING"]
    assert OutboxRelay(FakePublisher(), clock=clock).run_once().published == 1
    assert outbox_stats()["failed"] == 1


@pytest.mark.django_db(transaction=True)
def test_only_one_relay_is_active():
    enqueue(1)
    params = connection.get_connection_params()
    with psycopg.connect(**{k: v for k, v in params.items() if k != "cursor_factory"}) as other:
        other.execute("BEGIN")
        other.execute("SELECT pg_advisory_xact_lock(%s)", [RELAY_LOCK_ID])
        pub = FakePublisher()
        result = OutboxRelay(pub).run_once()
        assert result.leader is False and pub.sent == []
        other.execute("ROLLBACK")
    assert OutboxRelay(FakePublisher()).run_once().published == 1
