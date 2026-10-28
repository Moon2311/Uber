from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.drivers.models import AvailabilityStatus, DriverProfile
from apps.drivers.services import expire_stale_drivers
from apps.locations.models import DriverLocation
from apps.locations.stores import LocationFix, PostgresLocationStore
from apps.trips.models import Trip, TripStatus
from conftest import client_for, make_driver, make_rider

pytestmark = pytest.mark.django_db
AVAILABILITY = "/api/v1/drivers/availability/"
NEARBY = "/api/v1/drivers/nearby/"
CENTER = (37.7749, -122.4194)


def place(driver, lat=CENTER[0], lng=CENTER[1], seconds_ago=0):
    PostgresLocationStore().upsert_if_newer(
        LocationFix(
            driver_id=driver.pk,
            latitude=Decimal(str(lat)),
            longitude=Decimal(str(lng)),
            recorded_at=timezone.now() - timedelta(seconds=seconds_ago),
        )
    )


def age_status_change(driver, seconds):
    DriverProfile.objects.filter(pk=driver.pk).update(
        availability_changed_at=timezone.now() - timedelta(seconds=seconds)
    )


# --- availability -------------------------------------------------------------
def test_driver_goes_online_and_offline(offline_driver):
    client = client_for(offline_driver.user)
    response = client.post(AVAILABILITY, {"is_online": True}, format="json")
    assert response.status_code == 200 and response.json()["availability_status"] == "ONLINE"
    response = client.post(AVAILABILITY, {"is_online": False}, format="json")
    assert response.json()["availability_status"] == "OFFLINE"


def test_availability_requires_boolean(driver_client):
    assert (
        driver_client.post(AVAILABILITY, {"is_online": "maybe"}, format="json").status_code == 400
    )


def test_rider_cannot_set_availability(rider_client):
    assert rider_client.post(AVAILABILITY, {"is_online": True}, format="json").status_code == 403


def test_unauthenticated_cannot_set_availability(api_client):
    assert api_client.post(AVAILABILITY, {"is_online": True}, format="json").status_code == 401


def test_cannot_go_online_without_active_vehicle(offline_driver):
    offline_driver.vehicles.update(is_active=False)
    client = client_for(offline_driver.user)
    assert client.post(AVAILABILITY, {"is_online": True}, format="json").status_code == 409


def test_cannot_go_offline_during_active_trip(driver, driver_client):
    Trip.objects.create(
        rider=make_rider(),
        driver=driver,
        status=TripStatus.DRIVER_ASSIGNED,
        pickup_latitude=1,
        pickup_longitude=1,
        dropoff_latitude=2,
        dropoff_longitude=2,
        ride_category="ECONOMY",
        estimated_fare=10,
        estimated_distance_km=1,
        estimated_duration_minutes=3,
        currency="USD",
    )
    assert driver_client.post(AVAILABILITY, {"is_online": False}, format="json").status_code == 409


def test_going_offline_clears_latest_location(driver, driver_client):
    place(driver)
    driver_client.post(AVAILABILITY, {"is_online": False}, format="json")
    assert not DriverLocation.objects.filter(driver=driver).exists()


def test_availability_exposes_location_freshness(driver, driver_client, settings):
    settings.DRIVER_LOCATION_STALE_SECONDS = 120
    body = driver_client.get(AVAILABILITY).json()
    assert body["is_online"] is True and body["is_available"] is False
    assert body["location_freshness"] == "MISSING"
    place(driver)
    body = driver_client.get(AVAILABILITY).json()
    assert body["is_available"] is True and body["location_freshness"] == "FRESH"
    DriverLocation.objects.filter(driver=driver).update(
        recorded_at=timezone.now() - timedelta(minutes=5)
    )
    body = driver_client.get(AVAILABILITY).json()
    assert body["is_available"] is False and body["location_freshness"] == "STALE"


# --- stale expiry ----------------------------------------------------------------
def test_expire_stale_drivers(settings):
    settings.DRIVER_AUTO_OFFLINE_SECONDS = 600
    fresh, stale, silent, just_online = (make_driver(online=True) for _ in range(4))
    for d in (fresh, stale, silent):
        age_status_change(d, 3600)
    age_status_change(just_online, 10)
    place(fresh, seconds_ago=30)
    place(stale, seconds_ago=900)
    # `silent` never reported; `just_online` is inside the grace period
    assert expire_stale_drivers() == 2
    statuses = dict(DriverProfile.objects.values_list("pk", "availability_status"))
    assert statuses[fresh.pk] == AvailabilityStatus.ONLINE
    assert statuses[just_online.pk] == AvailabilityStatus.ONLINE
    assert statuses[stale.pk] == statuses[silent.pk] == AvailabilityStatus.OFFLINE
    assert not DriverLocation.objects.filter(driver=stale).exists()


def test_expire_stale_drivers_keeps_driver_on_trip(settings, recorded_metrics):
    settings.DRIVER_AUTO_OFFLINE_SECONDS = 600
    d = make_driver(online=True)
    age_status_change(d, 3600)
    Trip.objects.create(
        rider=make_rider(),
        driver=d,
        status=TripStatus.IN_PROGRESS,
        pickup_latitude=1,
        pickup_longitude=1,
        dropoff_latitude=2,
        dropoff_longitude=2,
        ride_category="ECONOMY",
        estimated_fare=10,
        estimated_distance_km=1,
        estimated_duration_minutes=3,
        currency="USD",
    )
    assert expire_stale_drivers() == 0
    assert recorded_metrics.count("driver.stale_on_trip") == 1


def test_expire_stale_drivers_command(settings, capsys):
    settings.DRIVER_AUTO_OFFLINE_SECONDS = 60
    d = make_driver(online=True)
    age_status_change(d, 3600)
    call_command("expire_stale_drivers")
    assert "Switched 1" in capsys.readouterr().out


# --- nearby ----------------------------------------------------------------------
def nearby(client, **params):
    return client.get(NEARBY, {"lat": CENTER[0], "lng": CENTER[1], **params})


def test_nearby_returns_only_available_fresh_drivers_sorted(rider_client, settings):
    settings.DRIVER_LOCATION_STALE_SECONDS = 120
    close, far, offline, stale, out_of_range = (make_driver(online=True) for _ in range(5))
    place(close, CENTER[0] + 0.001)
    place(far, CENTER[0] + 0.02)
    place(offline)
    DriverProfile.objects.filter(pk=offline.pk).update(availability_status="OFFLINE")
    place(stale, seconds_ago=600)
    place(out_of_range, CENTER[0] + 1)
    body = nearby(rider_client, radius_km=5).json()
    assert [r["id"] for r in body["results"]] == [str(close.public_id), str(far.public_id)]
    assert body["count"] == 2


def test_nearby_filters_by_category(rider_client):
    economy, xl = make_driver(online=True), make_driver(online=True, category="XL")
    place(economy)
    place(xl)
    body = nearby(rider_client, category="XL").json()
    assert [r["id"] for r in body["results"]] == [str(xl.public_id)]


def test_nearby_exposes_no_personal_data(rider_client):
    place(make_driver(online=True))
    result = nearby(rider_client).json()["results"][0]
    assert set(result) == {"id", "lat", "lng", "heading", "distance_km", "category"}


@pytest.mark.parametrize(
    "params", [{"lat": 100}, {"lng": -200}, {"lat": "x"}, {"radius_km": 0}, {"radius_km": 500}]
)
def test_nearby_validates_input(rider_client, params):
    assert nearby(rider_client, **params).status_code == 400


def test_nearby_forbidden_for_drivers(driver_client):
    assert nearby(driver_client).status_code == 403


def test_nearby_requires_auth(api_client):
    assert nearby(api_client).status_code == 401


def test_nearby_respects_result_limit(rider_client, settings):
    settings.NEARBY_DRIVERS_MAX_RESULTS = 2
    for _ in range(4):
        place(make_driver(online=True))
    assert nearby(rider_client).json()["count"] == 2
