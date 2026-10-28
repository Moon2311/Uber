"""Sampled DriverLocationUpdated stream: volume control and failure isolation."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.events import location_stream
from apps.events.schemas import decode
from apps.locations.stores import LocationFix

from .fakes import FakePublisher

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("redis_client")]
A, B = "8828308281fffff", "8828308283fffff"


@pytest.fixture
def pub(settings, monkeypatch):
    settings.KAFKA_ENABLED = True
    settings.LOCATION_EVENTS_ENABLED = True
    settings.LOCATION_EVENT_MIN_INTERVAL_SECONDS = 15
    fake = FakePublisher()
    monkeypatch.setattr("apps.events.kafka.get_publisher", lambda: fake)
    return fake


def fix(cell, driver_id=1, seconds_ago=0):
    return LocationFix(driver_id, Decimal("37.77"), Decimal("-122.41"),
                       timezone.now() - timedelta(seconds=seconds_ago), h3_cell=cell,
                       h3_resolution=8)  # fmt: skip


def reasons(pub):
    return [decode(m.value).payload["reason"] for m in pub.sent]


def test_pings_in_same_cell_are_sampled(pub):
    for _ in range(20):
        location_stream.publish_sampled_location(fix(A))
    assert reasons(pub) == ["interval"]  # 1 event, not 20


def test_cell_change_publishes_immediately(pub):
    location_stream.publish_sampled_location(fix(A))
    location_stream.publish_sampled_location(fix(A))
    location_stream.publish_sampled_location(fix(B))
    assert reasons(pub) == ["interval", "cell_changed"]


def test_interval_elapsed_publishes_again(pub, redis_client, settings):
    location_stream.publish_sampled_location(fix(A))
    key = f"{settings.REDIS_KEY_PREFIX}:{{evt}}:locgate:1"
    redis_client.delete(key)  # gate TTL (15 s) elapsed
    location_stream.publish_sampled_location(fix(A))
    assert reasons(pub) == ["interval", "interval"]


def test_drivers_are_gated_independently(pub):
    location_stream.publish_sampled_location(fix(A, driver_id=1))
    location_stream.publish_sampled_location(fix(A, driver_id=2))
    assert [m.key for m in pub.sent] == ["1", "2"]


def test_message_shape(pub):
    location_stream.publish_sampled_location(fix(A, driver_id=9))
    [m] = pub.sent
    event = decode(m.value)
    assert m.topic == "driver.location.v1" and m.key == "9"
    assert event.payload["h3_cell"] == A and event.payload["lat"] == "37.77"


def test_redis_down_skips_event_without_error(pub, settings, recorded_metrics):
    settings.REDIS_URL = "redis://localhost:1/0"
    assert location_stream.publish_sampled_location(fix(A)) is False
    assert pub.sent == [] and recorded_metrics.count("events.location.gate_unavailable") == 1


def test_full_producer_queue_drops_event(pub, monkeypatch, recorded_metrics):
    monkeypatch.setattr(pub, "produce_nowait", lambda m: False)
    assert location_stream.publish_sampled_location(fix(A)) is False
    assert recorded_metrics.count("events.location.dropped") == 1


def test_disabled(pub, settings):
    settings.LOCATION_EVENTS_ENABLED = False
    assert location_stream.publish_sampled_location(fix(A)) is False


def test_gps_update_survives_event_failure(pub, monkeypatch, driver_client):
    def boom(fix):
        raise RuntimeError("kafka client exploded")

    monkeypatch.setattr(location_stream, "publish_sampled_location", boom)
    r = driver_client.post("/api/v1/locations/update/", {"lat": 1, "lng": 2}, format="json")
    assert r.status_code == 200 and r.json()["accepted"] is True


def test_location_api_emits_sampled_events(pub, driver_client):
    now = timezone.now()
    for i in range(5):  # same cell, 1 s apart
        ts = (now - timedelta(seconds=5 - i)).isoformat()
        payload = {"lat": 37.7749, "lng": -122.4194, "recorded_at": ts}
        driver_client.post("/api/v1/locations/update/", payload, format="json")
    assert len(pub.sent) == 1
