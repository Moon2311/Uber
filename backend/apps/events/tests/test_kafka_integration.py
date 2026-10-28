"""End-to-end against a real Kafka broker (docker compose: localhost:9094)."""

import time
import uuid

import pytest
from confluent_kafka import Producer
from django.urls import reverse

from apps.analytics.models import TripTimelineEntry
from apps.events.consumer import ConsumerRunner
from apps.events.kafka import KafkaPublisher, consumer_lag, get_publisher, make_consumer
from apps.events.models import OutboxEvent, OutboxStatus, ProcessedEvent
from apps.events.registry import group_config, physical_group
from apps.events.relay import OutboxRelay
from apps.events.schemas import decode
from apps.events.topics import DEAD_LETTER, DRIVER_LOCATION, TRIP_EVENTS, physical

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("kafka_topics")]

TRIP = {
    "pickup": {"lat": 37.7749, "lng": -122.4194},
    "dropoff": {"lat": 37.8044, "lng": -122.2712},
    "ride_category": "ECONOMY",
}


def run_analytics(max_messages=None, idle_timeout=8.0):
    topics, handlers = group_config("analytics")
    ConsumerRunner(physical_group("analytics"), topics, handlers).run(
        max_messages=max_messages, idle_timeout=idle_timeout
    )


def read_all(logical_topic, expect, timeout=15.0):
    consumer = make_consumer(f"reader-{uuid.uuid4().hex[:6]}")
    consumer.subscribe([physical(logical_topic)])
    out, deadline = [], time.monotonic() + timeout
    try:
        while len(out) < expect and time.monotonic() < deadline:
            m = consumer.poll(0.5)
            if m is not None and not m.error():
                out.append(m)
    finally:
        consumer.close()
    return out


def lag():
    return consumer_lag(physical_group("analytics"), [TRIP_EVENTS])["total_lag"]


def create_and_cancel(client):
    trip_id = client.post(reverse("v1:trip-create"), TRIP, format="json").json()["id"]
    client.post(reverse("v1:trip-cancel", kwargs={"trip_id": trip_id}), {})
    return trip_id


def test_outbox_to_kafka_to_consumer_end_to_end(rider_client):
    trip_id = create_and_cancel(rider_client)
    result = OutboxRelay(get_publisher()).run_once()
    assert result.published == 3  # + PaymentStatusChanged (PENDING -> CANCELLED)
    assert set(OutboxEvent.objects.values_list("status", flat=True)) == {OutboxStatus.PUBLISHED}

    msgs = read_all(TRIP_EVENTS, expect=2)
    assert [m.key().decode() for m in msgs] == [trip_id, trip_id]
    assert len({m.partition() for m in msgs}) == 1  # same key -> same partition -> ordered
    assert [decode(m.value()).event_type for m in msgs] == ["RideRequested", "TripStatusChanged"]

    assert lag() == 2  # analytics has not consumed yet
    run_analytics(max_messages=2)
    statuses = TripTimelineEntry.objects.filter(trip_id=trip_id).values_list("status", flat=True)
    assert list(statuses) == ["REQUESTED", "CANCELLED"]
    assert lag() == 0


def test_duplicate_publication_is_absorbed_by_consumer(rider_client):
    trip_id = create_and_cancel(rider_client)
    relay = OutboxRelay(get_publisher())
    relay.run_once()
    # Relay crashed before marking rows -> publishes the same events again.
    OutboxEvent.objects.update(status=OutboxStatus.PENDING, published_at=None)
    assert relay.run_once().published == 3
    assert len(read_all(TRIP_EVENTS, expect=4)) == 4  # Kafka really holds duplicates

    run_analytics(max_messages=4)
    assert TripTimelineEntry.objects.filter(trip_id=trip_id).count() == 2
    assert ProcessedEvent.objects.filter(consumer_group=physical_group("analytics")).count() == 2
    assert lag() == 0


def test_poison_message_goes_to_dead_letter_topic():
    raw = Producer({"bootstrap.servers": "localhost:9094"})
    raw.produce(physical(TRIP_EVENTS), key="bad", value=b"{not json")
    raw.flush(10)
    run_analytics(max_messages=1)
    [dead] = read_all(DEAD_LETTER, expect=1)
    h = dict(dead.headers())
    assert dead.value() == b"{not json" and dead.key() == b"bad"
    assert h["dlq_original_topic"].decode() == physical(TRIP_EVENTS)
    assert h["dlq_error_class"] == b"SchemaError"
    assert lag() == 0  # committed past the poison message: the stream is not blocked


def test_outbox_recovers_after_kafka_outage(rider_client, settings):
    settings.KAFKA_DELIVERY_TIMEOUT_SECONDS = 2
    settings.OUTBOX_RETRY_BASE_SECONDS = 0
    create_and_cancel(rider_client)
    dead_broker = KafkaPublisher(
        {"bootstrap.servers": "localhost:1", "message.timeout.ms": 1500, "log_level": 0}
    )
    result = OutboxRelay(dead_broker).run_once()
    del dead_broker  # stop librdkafka reconnect attempts
    assert result.published == 0 and result.failed == 1
    row = OutboxEvent.objects.order_by("id").first()
    assert row.status == OutboxStatus.PENDING and row.attempts == 1 and row.last_error

    assert OutboxRelay(get_publisher()).run_once().published == 3  # broker back
    assert len(read_all(TRIP_EVENTS, expect=2)) == 2


def test_health_reports_kafka_and_lag(rider_client, settings):
    from django.contrib.auth import get_user_model

    create_and_cancel(rider_client)
    OutboxRelay(get_publisher()).run_once()
    body = rider_client.get("/api/v1/health/").json()
    assert body["events"]["kafka"]["status"] == "ok" and body["events"]["outbox"]["pending"] == 0

    staff = get_user_model().objects.create_user(
        email="ops@example.com", password="x", role="RIDER", is_staff=True
    )
    from conftest import client_for

    report = client_for(staff).get("/api/v1/health/events/").json()
    assert report["consumer_groups"]["analytics"]["total_lag"] == 2
    assert "consumer_down:analytics" in report["problems"]  # no heartbeat yet
    assert rider_client.get("/api/v1/health/events/").status_code == 403

    settings.KAFKA_BOOTSTRAP_SERVERS = "localhost:1"
    degraded = rider_client.get("/api/v1/health/")
    assert degraded.status_code == 200 and degraded.json()["status"] == "degraded"
    assert "kafka_unreachable" in degraded.json()["events"]["problems"]


def test_sampled_location_events_reach_kafka(driver_client, settings):
    settings.LOCATION_EVENTS_ENABLED = True
    for lat in (37.7749, 37.7750, 37.7751, 37.80):  # 3 in one cell, then a new cell
        driver_client.post("/api/v1/locations/update/", {"lat": lat, "lng": -122.4194},
                           format="json")  # fmt: skip
    get_publisher().flush(10)
    msgs = read_all(DRIVER_LOCATION, expect=2)
    assert [decode(m.value()).payload["reason"] for m in msgs] == ["interval", "cell_changed"]
