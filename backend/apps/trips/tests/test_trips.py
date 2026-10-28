from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.payments.models import PaymentRecord, PaymentStatus
from apps.trips.models import Trip, TripStatus
from conftest import client_for, make_driver, make_rider

pytestmark = pytest.mark.django_db

PAYLOAD = {
    "pickup": {"lat": 37.7749, "lng": -122.4194, "address": "Market St"},
    "dropoff": {"lat": 37.8044, "lng": -122.2712, "address": "Oakland"},
    "ride_category": "ECONOMY",
}


def create(client, payload=None):
    return client.post(reverse("v1:trip-create"), payload or PAYLOAD, format="json")


def test_rider_creates_trip_with_estimate_and_pending_payment(rider_client, rider):
    response = create(rider_client)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "REQUESTED" and body["driver"] is None
    assert Decimal(body["estimated_fare"]) > 0
    payment = PaymentRecord.objects.get(trip_id=body["id"])
    assert payment.status == PaymentStatus.PENDING and payment.rider == rider


def test_rider_cannot_assign_driver_or_set_status(rider_client, driver):
    payload = {**PAYLOAD, "driver": str(driver.public_id), "status": "COMPLETED"}
    response = create(rider_client, payload)
    assert response.status_code == 400
    assert set(response.json()["error"]["details"]) == {"driver", "status"}
    assert not Trip.objects.exists()


@pytest.mark.parametrize(
    "pickup",
    [{"lat": 91, "lng": 0}, {"lat": 0, "lng": -181}, {"lat": "abc", "lng": 0}, {"lat": 10}],
)
def test_invalid_coordinates_rejected(rider_client, pickup):
    response = create(rider_client, {**PAYLOAD, "pickup": pickup})
    assert response.status_code == 400
    assert "pickup" in response.json()["error"]["details"]


def test_pickup_equal_to_dropoff_rejected(rider_client):
    response = create(rider_client, {**PAYLOAD, "dropoff": PAYLOAD["pickup"]})
    assert response.status_code == 400


def test_only_one_active_trip_per_rider(rider_client):
    assert create(rider_client).status_code == 201
    response = create(rider_client)
    assert response.status_code == 409 and response.json()["error"]["code"] == "conflict"


def test_driver_cannot_request_trip(driver_client):
    assert create(driver_client).status_code == 403


def test_unauthenticated_cannot_request_trip(api_client):
    assert create(api_client).status_code == 401


def test_trip_ownership(rider_client):
    trip_id = create(rider_client).json()["id"]
    url = reverse("v1:trip-detail", kwargs={"trip_id": trip_id})
    assert rider_client.get(url).status_code == 200
    other = client_for(make_rider())
    assert other.get(url).status_code == 404
    assert client_for(make_driver().user).get(url).status_code == 404


def test_history_is_scoped_and_paginated(rider_client):
    create(rider_client)
    make_rider_client = client_for(make_rider())
    create(make_rider_client)
    response = rider_client.get(reverse("v1:trip-history"))
    body = response.json()
    assert response.status_code == 200 and body["count"] == 1 and "results" in body


def test_rider_can_cancel_and_payment_is_cancelled(rider_client):
    trip_id = create(rider_client).json()["id"]
    response = rider_client.post(reverse("v1:trip-cancel", kwargs={"trip_id": trip_id}), {})
    assert response.status_code == 200 and response.json()["status"] == "CANCELLED"
    assert PaymentRecord.objects.get(trip_id=trip_id).status == PaymentStatus.CANCELLED
    # cancelled trips free the rider to request again
    assert create(rider_client).status_code == 201


def test_other_rider_cannot_cancel(rider_client):
    trip_id = create(rider_client).json()["id"]
    other = client_for(make_rider())
    assert other.post(reverse("v1:trip-cancel", kwargs={"trip_id": trip_id}), {}).status_code == 404


def _trip(**overrides):
    fields = {
        "rider": overrides.pop("rider", None) or make_rider(),
        "pickup_latitude": Decimal("1"),
        "pickup_longitude": Decimal("1"),
        "dropoff_latitude": Decimal("2"),
        "dropoff_longitude": Decimal("2"),
        "ride_category": "ECONOMY",
        "estimated_fare": Decimal("10.00"),
        "estimated_distance_km": Decimal("1"),
        "estimated_duration_minutes": 5,
        "currency": "USD",
        **overrides,
    }
    return Trip.objects.create(**fields)


@pytest.mark.parametrize(
    "overrides",
    [
        {"pickup_latitude": Decimal("95")},
        {"estimated_fare": Decimal("-1")},
        {"status": TripStatus.IN_PROGRESS},  # no driver
        {"status": "BOGUS"},
        {"dropoff_latitude": Decimal("1"), "dropoff_longitude": Decimal("1")},
    ],
)
def test_database_constraints(overrides):
    with pytest.raises(IntegrityError), transaction.atomic():
        _trip(**overrides)


def test_db_enforces_one_active_trip_per_rider():
    rider = make_rider()
    _trip(rider=rider)
    with pytest.raises(IntegrityError), transaction.atomic():
        _trip(rider=rider)


def test_db_enforces_one_active_trip_per_driver():
    driver = make_driver(online=True)
    _trip(driver=driver, status=TripStatus.DRIVER_ASSIGNED)
    with pytest.raises(IntegrityError), transaction.atomic():
        _trip(driver=driver, status=TripStatus.IN_PROGRESS)
