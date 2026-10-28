import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from apps.events import builders, topics
from apps.events.schemas import SCHEMAS, SchemaError, decode, make_event, schema_for

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
TRIP = uuid.uuid4()

SAMPLES = {
    "DriverLocationUpdated": {"driver_id": 7, "lat": Decimal("37.1"), "lng": Decimal("-122.2"),
                              "recorded_at": NOW, "h3_cell": "8828308281fffff",
                              "h3_resolution": 8, "reason": "interval"},
    "DriverAvailabilityChanged": {"driver_id": 7, "status": "ONLINE", "previous_status": "OFFLINE",
                                  "reason": "driver_request", "vehicle_category": "XL",
                                  "changed_at": NOW},
    "RideRequested": {"trip_id": TRIP, "rider_id": 3, "ride_category": "ECONOMY",
                      "pickup_lat": Decimal("1"), "pickup_lng": Decimal("2"),
                      "dropoff_lat": Decimal("3"), "dropoff_lng": Decimal("4"),
                      "pickup_h3_cell": None, "estimated_fare": Decimal("12.50"),
                      "currency": "USD", "requested_at": NOW},
    "RideOfferCreated": {"trip_id": TRIP, "offer_id": uuid.uuid4(), "driver_id": 7,
                         "expires_at": NOW, "straight_line_km": 1.2, "grid_distance": 2,
                         "created_at": NOW},
    "RideAccepted": {"trip_id": TRIP, "offer_id": uuid.uuid4(), "driver_id": 7, "accepted_at": NOW},
    "TripStatusChanged": {"trip_id": TRIP, "from_status": "REQUESTED", "to_status": "CANCELLED",
                          "rider_id": 3, "driver_id": None, "changed_at": NOW},
    "TripCompleted": {"trip_id": TRIP, "rider_id": 3, "driver_id": 7, "ride_category": "ECONOMY",
                      "estimated_fare": Decimal("12.50"), "final_fare": None, "currency": "USD",
                      "estimated_distance_km": Decimal("4.2"), "started_at": NOW,
                      "completed_at": NOW},
}  # fmt: skip


def test_all_required_event_types_have_v1_schemas():
    assert set(SAMPLES) == {t for t, _ in SCHEMAS}
    assert all(v == 1 for _, v in SCHEMAS)


@pytest.mark.parametrize("event_type", sorted(SAMPLES))
def test_roundtrip(event_type):
    event = make_event(event_type, SAMPLES[event_type])
    decoded = decode(event.to_bytes())
    assert decoded == event
    assert decoded.envelope()["producer"] == "ride-hailing-backend"
    json.dumps(event.schema.json_schema())  # exportable


@pytest.mark.parametrize(
    "event_type, key_field, topic",
    [
        ("DriverLocationUpdated", "driver_id", topics.DRIVER_LOCATION),
        ("DriverAvailabilityChanged", "driver_id", topics.DRIVER_AVAILABILITY),
        ("RideRequested", "trip_id", topics.TRIP_EVENTS),
        ("RideOfferCreated", "trip_id", topics.TRIP_EVENTS),
        ("RideAccepted", "trip_id", topics.TRIP_EVENTS),
        ("TripStatusChanged", "trip_id", topics.TRIP_EVENTS),
        ("TripCompleted", "trip_id", topics.TRIP_EVENTS),
    ],
)
def test_keys_and_topics(event_type, key_field, topic):
    event = make_event(event_type, SAMPLES[event_type])
    assert event.key == str(SAMPLES[event_type][key_field]) and event.topic == topic


def test_money_is_a_decimal_string_and_floats_are_rejected():
    event = make_event("RideRequested", SAMPLES["RideRequested"])
    assert event.payload["estimated_fare"] == "12.50"
    with pytest.raises(SchemaError):
        make_event("RideRequested", {**SAMPLES["RideRequested"], "estimated_fare": 12.5})


@pytest.mark.parametrize(
    "change",
    [
        {"driver_id": "7"},  # str for int
        {"driver_id": True},  # bool is not int
        {"changed_at": "2026-10-28T12:00:00"},  # naive
        {"changed_at": "yesterday"},
        {"status": None},  # required, not nullable
    ],
)
def test_invalid_payloads(change):
    with pytest.raises(SchemaError):
        make_event("DriverAvailabilityChanged", {**SAMPLES["DriverAvailabilityChanged"], **change})


def test_missing_required_field():
    payload = dict(SAMPLES["RideAccepted"])
    del payload["offer_id"]
    with pytest.raises(SchemaError):
        make_event("RideAccepted", payload)


def test_unknown_fields_are_dropped_for_forward_compatibility():
    raw = json.loads(make_event("RideAccepted", SAMPLES["RideAccepted"]).to_bytes())
    raw["payload"]["added_in_v1_1"] = "x"
    assert "added_in_v1_1" not in decode(json.dumps(raw).encode()).payload


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"event_type": "RideAccepted"}',
        json.dumps({"event_id": "nope", "event_type": "RideAccepted", "event_version": 1,
                    "occurred_at": "x", "payload": {}}).encode(),
        json.dumps({"event_id": str(uuid.uuid4()), "event_type": "RideAccepted",
                    "event_version": 99, "occurred_at": "x", "payload": {}}).encode(),
        json.dumps({"event_id": str(uuid.uuid4()), "event_type": "Nope",
                    "event_version": 1, "occurred_at": "x", "payload": {}}).encode(),
    ],
)  # fmt: skip
def test_decode_rejects_bad_envelopes(raw):
    with pytest.raises(SchemaError):
        decode(raw)


def test_unknown_version_rejected():
    with pytest.raises(SchemaError):
        schema_for("TripCompleted", 2)


def test_offer_builders_for_matching_step():
    e = builders.ride_offer_created(
        trip_id=TRIP, offer_id=uuid.uuid4(), driver_id=7, expires_at=NOW, created_at=NOW
    )
    assert e.key == str(TRIP)
    a = builders.ride_accepted(trip_id=TRIP, offer_id=uuid.uuid4(), driver_id=7, accepted_at=NOW)
    assert a.event_type == "RideAccepted"
