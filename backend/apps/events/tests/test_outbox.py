"""Business operations write their events to the outbox in the same transaction."""

import pytest
from django.db import transaction
from django.urls import reverse

from apps.drivers.services import set_availability
from apps.events import builders, outbox
from apps.events.models import OutboxEvent
from apps.trips import state_machine
from apps.trips.models import Trip, TripStatus
from conftest import make_driver

pytestmark = pytest.mark.django_db

TRIP = {
    "pickup": {"lat": 37.7749, "lng": -122.4194},
    "dropoff": {"lat": 37.8044, "lng": -122.2712},
    "ride_category": "ECONOMY",
}


def types():
    return list(OutboxEvent.objects.order_by("id").values_list("event_type", flat=True))


def test_ride_request_writes_ride_requested(rider_client, rider):
    trip_id = rider_client.post(reverse("v1:trip-create"), TRIP, format="json").json()["id"]
    [row] = OutboxEvent.objects.all()
    assert row.event_type == "RideRequested" and row.key == trip_id
    assert row.topic == "trip.events.v1" and row.payload["rider_id"] == rider.pk
    assert row.payload["pickup_h3_cell"] and row.payload["estimated_fare"]


def test_cancel_writes_status_change_with_reason(rider_client):
    trip_id = rider_client.post(reverse("v1:trip-create"), TRIP, format="json").json()["id"]
    rider_client.post(reverse("v1:trip-cancel", kwargs={"trip_id": trip_id}), {"reason": "late"})
    row = OutboxEvent.objects.get(event_type="TripStatusChanged")
    assert row.key == trip_id
    assert row.payload["from_status"] == "REQUESTED" and row.payload["to_status"] == "CANCELLED"
    assert row.payload["reason"] == "late"


def test_completion_writes_status_change_and_trip_completed(rider):
    driver = make_driver(online=True)
    trip = Trip.objects.create(
        rider=rider, driver=driver, status=TripStatus.DRIVER_ARRIVING, pickup_latitude=1,
        pickup_longitude=1, dropoff_latitude=2, dropoff_longitude=2, ride_category="ECONOMY",
        estimated_fare="9.99", estimated_distance_km=3, estimated_duration_minutes=5,
        currency="USD",
    )  # fmt: skip
    with transaction.atomic():
        actor = state_machine.Actor.driver(driver)
        state_machine.transition(trip, TripStatus.IN_PROGRESS, actor=actor)
        state_machine.transition(trip, TripStatus.COMPLETED, actor=actor)
    assert types() == ["TripStatusChanged", "TripStatusChanged", "TripCompleted"]
    done = OutboxEvent.objects.get(event_type="TripCompleted")
    assert done.payload["driver_id"] == driver.pk and done.payload["estimated_fare"] == "9.99"


def test_availability_change_is_keyed_by_driver():
    driver = make_driver(online=False)
    set_availability(driver=driver, online=True)
    set_availability(driver=driver, online=True)  # no change -> no event
    set_availability(driver=driver, online=False)
    rows = list(OutboxEvent.objects.order_by("id"))
    assert [r.payload["status"] for r in rows] == ["ONLINE", "OFFLINE"]
    assert rows[1].payload["previous_status"] == "ONLINE"
    assert {r.key for r in rows} == {str(driver.pk)} and rows[0].topic == "driver.availability.v1"


def test_rolled_back_business_change_leaves_no_event(rider_client):
    rider_client.post(reverse("v1:trip-create"), TRIP, format="json")
    # second request fails with 409 inside its transaction -> nothing written
    assert rider_client.post(reverse("v1:trip-create"), TRIP, format="json").status_code == 409
    assert types() == ["RideRequested"]


def test_event_and_change_roll_back_together():
    driver = make_driver(online=False)
    with pytest.raises(RuntimeError), transaction.atomic():
        set_availability(driver=driver, online=True)
        raise RuntimeError("later step failed")
    driver.refresh_from_db()
    assert driver.availability_status == "OFFLINE" and not OutboxEvent.objects.exists()


def test_gps_updates_do_not_use_the_outbox(driver_client):
    for lat in (37.77, 37.78, 37.79):
        driver_client.post("/api/v1/locations/update/", {"lat": lat, "lng": -122.4}, format="json")
    assert not OutboxEvent.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_enqueue_outside_a_transaction_is_refused():
    event = builders.driver_availability_changed(
        driver_id=1, status="ONLINE", previous_status="OFFLINE", reason="x",
        vehicle_category=None, changed_at="2026-10-28T12:00:00+00:00",
    )  # fmt: skip
    with pytest.raises(RuntimeError):
        outbox.enqueue(event)
