"""Location update API (step 2)."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.locations.models import DriverLocationEvent
from conftest import client_for, latest, make_driver

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("live_store_backend")]
URL = "/api/v1/locations/update/"


def post(client, **fields):
    payload = {"lat": 37.7749, "lng": -122.4194, **fields}
    return client.post(URL, payload, format="json")


def iso(dt):
    return dt.isoformat()


def test_url_is_stable():
    assert reverse("v1:location-update") == URL


def test_full_update_is_stored(driver_client, driver):
    ts = timezone.now() - timedelta(seconds=2)
    response = post(driver_client, accuracy_m=5.5, heading=90, speed_kmh=32.4, recorded_at=iso(ts))
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True and body["outcome"] == "applied"
    assert body["freshness"] == "FRESH"
    # step-1 response fields are still present at top level
    for key in ["lat", "lng", "heading", "speed_kmh", "accuracy_m", "recorded_at", "updated_at"]:
        assert key in body
    row = latest(driver)
    assert row.accuracy_m == 5.5 and row.heading == 90 and row.speed_kmh == 32.4
    assert row.recorded_at == ts


def test_minimal_update_defaults_timestamp_to_server_time(driver_client, driver):
    before = timezone.now()
    assert post(driver_client).status_code == 200
    assert latest(driver).recorded_at >= before


def test_high_precision_gps_is_rounded(driver_client, driver):
    response = post(driver_client, lat=37.774929123456789, lng=-122.419416123456789)
    assert response.status_code == 200
    row = latest(driver)
    assert str(row.latitude) == "37.774929" and str(row.longitude) == "-122.419416"


@pytest.mark.parametrize(
    "fields, bad_field",
    [
        ({"lat": 90.5}, "lat"),
        ({"lat": -91}, "lat"),
        ({"lng": 180.1}, "lng"),
        ({"lng": "east"}, "lng"),
        ({"lat": None}, "lat"),
        ({"heading": 360}, "heading"),
        ({"heading": -1}, "heading"),
        ({"speed_kmh": -5}, "speed_kmh"),
        ({"accuracy_m": -1}, "accuracy_m"),
        ({"recorded_at": "yesterday"}, "recorded_at"),
    ],
)
def test_invalid_fields_rejected(driver_client, fields, bad_field):
    response = post(driver_client, **fields)
    assert response.status_code == 400
    assert bad_field in response.json()["error"]["details"]


def test_missing_coordinates_rejected(driver_client):
    response = driver_client.post(URL, {}, format="json")
    assert response.status_code == 400
    assert {"lat", "lng"} <= set(response.json()["error"]["details"])


def test_future_timestamp_rejected(driver_client, settings):
    settings.LOCATION_MAX_FUTURE_SKEW_SECONDS = 30
    response = post(driver_client, recorded_at=iso(timezone.now() + timedelta(minutes=2)))
    assert response.status_code == 400
    assert "recorded_at" in response.json()["error"]["details"]


def test_naive_timestamp_rejected(driver_client):
    response = post(driver_client, recorded_at="2026-01-01T10:00:00")
    assert response.status_code == 400


def test_small_future_skew_is_clamped_to_server_time(driver_client, driver):
    future = timezone.now() + timedelta(seconds=10)
    assert post(driver_client, recorded_at=iso(future)).json()["accepted"] is True
    assert latest(driver).recorded_at < future


# --- ownership / roles -------------------------------------------------------
def test_driver_id_in_body_is_rejected(driver_client):
    victim = make_driver(online=True)
    response = post(driver_client, driver_id=victim.pk)
    assert response.status_code == 400
    assert "driver_id" in response.json()["error"]["details"]
    assert latest(victim) is None


def test_driver_only_writes_own_location(driver_client, driver):
    other = make_driver(online=True)
    post(driver_client)
    assert latest(driver) is not None
    assert latest(other) is None


def test_rider_cannot_update_location(rider_client):
    assert post(rider_client).status_code == 403


def test_unauthenticated_cannot_update_location(api_client):
    assert post(api_client).status_code == 401


def test_offline_driver_rejected(offline_driver):
    response = post(client_for(offline_driver.user))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "driver_offline"
    assert latest(offline_driver) is None


# --- ordering ----------------------------------------------------------------
def test_out_of_order_update_is_ignored(driver_client, driver):
    now = timezone.now()
    assert post(driver_client, lat=10, recorded_at=iso(now - timedelta(seconds=5))).json()[
        "accepted"
    ]
    late = post(driver_client, lat=20, recorded_at=iso(now - timedelta(seconds=30)))
    assert late.status_code == 200
    body = late.json()
    assert body["accepted"] is False and body["outcome"] == "out_of_order"
    assert body["lat"] == 10  # response reflects what is actually stored
    assert float(latest(driver).latitude) == 10


def test_duplicate_timestamp_is_ignored(driver_client, driver):
    ts = iso(timezone.now() - timedelta(seconds=3))
    assert post(driver_client, lat=10, recorded_at=ts).json()["accepted"] is True
    assert post(driver_client, lat=11, recorded_at=ts).json()["outcome"] == "out_of_order"
    assert float(latest(driver).latitude) == 10


def test_newer_update_replaces_previous(driver_client, driver):
    now = timezone.now()
    for i, seconds_ago in enumerate([30, 20, 10]):
        post(driver_client, lat=10 + i, recorded_at=iso(now - timedelta(seconds=seconds_ago)))
    assert float(latest(driver).latitude) == 12


def test_too_old_update_not_applied(driver_client, driver, settings):
    settings.LOCATION_MAX_AGE_SECONDS = 60
    response = post(driver_client, recorded_at=iso(timezone.now() - timedelta(minutes=5)))
    assert response.status_code == 200
    assert response.json()["outcome"] == "too_old"
    assert response.json()["freshness"] == "MISSING"
    assert latest(driver) is None


def test_low_accuracy_update_not_applied(driver_client, driver, settings):
    settings.LOCATION_MAX_ACCURACY_M = 100
    response = post(driver_client, accuracy_m=2500)
    assert response.json()["outcome"] == "low_accuracy"
    assert latest(driver) is None


# --- history -----------------------------------------------------------------
def test_history_is_sampled_not_one_row_per_update(driver_client, driver, settings):
    settings.LOCATION_HISTORY_MIN_INTERVAL_SECONDS = 30
    settings.LOCATION_HISTORY_MIN_DISTANCE_M = 100
    settings.LOCATION_HISTORY_MAX_INTERVAL_SECONDS = 300
    start = timezone.now() - timedelta(seconds=200)
    # 20 pings every 4 s barely moving -> only the first creates an event
    for i in range(20):
        post(
            driver_client,
            lat=37.7749 + i * 0.00001,
            recorded_at=iso(start + timedelta(seconds=4 * i)),
        )
    assert DriverLocationEvent.objects.filter(driver=driver).count() == 1
    # >30 s later and ~1.1 km away -> a new event
    post(driver_client, lat=37.7849, recorded_at=iso(start + timedelta(seconds=120)))
    assert DriverLocationEvent.objects.filter(driver=driver).count() == 2


def test_history_heartbeat_when_stationary(driver_client, driver, settings):
    settings.LOCATION_HISTORY_MAX_INTERVAL_SECONDS = 60
    start = timezone.now() - timedelta(seconds=100)
    post(driver_client, recorded_at=iso(start))
    post(driver_client, recorded_at=iso(start + timedelta(seconds=30)))
    post(driver_client, recorded_at=iso(start + timedelta(seconds=61)))
    assert DriverLocationEvent.objects.filter(driver=driver).count() == 2


def test_history_can_be_disabled(driver_client, driver, settings):
    settings.LOCATION_HISTORY_ENABLED = False
    post(driver_client)
    assert latest(driver) is not None
    assert not DriverLocationEvent.objects.exists()


def test_ignored_updates_do_not_create_history(driver_client, settings):
    settings.LOCATION_MAX_ACCURACY_M = 100
    post(driver_client, accuracy_m=1000)
    assert not DriverLocationEvent.objects.exists()


# --- my location ---------------------------------------------------------------
def test_my_location_reports_freshness(driver_client, settings):
    assert driver_client.get("/api/v1/locations/me/").json()["freshness"] == "MISSING"
    post(driver_client, recorded_at=iso(timezone.now() - timedelta(seconds=90)))
    settings.DRIVER_LOCATION_STALE_SECONDS = 120
    body = driver_client.get("/api/v1/locations/me/").json()
    assert body["freshness"] == "FRESH" and body["location"]["lat"] == 37.7749
    settings.DRIVER_LOCATION_STALE_SECONDS = 60
    assert driver_client.get("/api/v1/locations/me/").json()["freshness"] == "STALE"


def test_my_location_forbidden_for_riders(rider_client):
    assert rider_client.get("/api/v1/locations/me/").status_code == 403


# --- observability -------------------------------------------------------------
def test_metrics_are_emitted(driver_client, recorded_metrics):
    now = timezone.now()
    post(driver_client, recorded_at=iso(now - timedelta(seconds=1)))
    post(driver_client, recorded_at=iso(now - timedelta(seconds=9)))
    assert recorded_metrics.count("location.update", outcome="applied") == 1
    assert recorded_metrics.count("location.update", outcome="out_of_order") == 1
    assert recorded_metrics.count("location.history.recorded") == 1
    assert recorded_metrics.timings


def test_offline_rejection_metric(offline_driver, recorded_metrics):
    post(client_for(offline_driver.user))
    assert recorded_metrics.count("location.update", outcome="rejected_offline") == 1


def test_structured_log_has_no_coordinates(driver_client, caplog):
    now = timezone.now()
    post(driver_client, lat=11.111111, recorded_at=iso(now))
    with caplog.at_level("INFO", logger="apps.locations"):
        post(driver_client, lat=22.222222, recorded_at=iso(now - timedelta(seconds=60)))
    records = [r for r in caplog.records if r.getMessage() == "location.update"]
    assert records and records[0].outcome == "out_of_order" and records[0].driver_id
    assert "22.22" not in caplog.text and "11.11" not in caplog.text


# --- H3 (step 3) ---------------------------------------------------------------
def test_update_stores_h3_cell_at_configured_resolution(driver_client, driver, settings):
    import h3

    settings.H3_RESOLUTION = 9
    post(driver_client, lat=37.7749, lng=-122.4194)
    row = latest(driver)
    assert row.h3_cell == h3.latlng_to_cell(37.7749, -122.4194, 9) and row.h3_resolution == 9


def test_cell_moves_with_the_driver(driver_client, driver):
    now = timezone.now()
    post(driver_client, lat=37.7749, recorded_at=iso(now - timedelta(seconds=5)))
    first = latest(driver).h3_cell
    post(driver_client, lat=37.80, recorded_at=iso(now))
    assert latest(driver).h3_cell != first


def test_out_of_order_update_does_not_change_cell(driver_client, driver):
    now = timezone.now()
    post(driver_client, lat=37.7749, recorded_at=iso(now))
    cell = latest(driver).h3_cell
    post(driver_client, lat=37.90, recorded_at=iso(now - timedelta(seconds=10)))
    assert latest(driver).h3_cell == cell


def test_h3_failure_never_loses_the_fix(driver_client, driver, monkeypatch, recorded_metrics):
    import h3

    def boom(*args):
        raise h3.H3FailedError("simulated")

    monkeypatch.setattr(h3, "latlng_to_cell", boom)
    assert post(driver_client).json()["accepted"] is True
    row = latest(driver)
    assert row.h3_cell is None and row.latitude is not None
    assert recorded_metrics.count("location.h3.index_error") == 1
