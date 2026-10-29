import logging

import pytest
from django.urls import reverse

from apps.common.geo import bounding_box, haversine_km
from apps.common.logging import JsonFormatter, redact


@pytest.mark.django_db
def test_health_ok(api_client):
    from conftest import client_for, make_rider

    response = api_client.get(reverse("v1:health"))
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    body = client_for(make_rider(is_staff=True)).get(reverse("v1:health")).json()
    assert body["status"] == "ok" and body["database"] == "ok"
    assert body["live_store"]["status"] == "ok"


@pytest.mark.django_db
def test_error_envelope_for_unauthenticated(api_client):
    response = api_client.get(reverse("v1:auth-me"))
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "not_authenticated"
    assert "message" in body["error"]


def test_haversine_known_distance():
    # San Francisco -> Los Angeles ~ 559 km
    assert 550 < haversine_km(37.7749, -122.4194, 34.0522, -118.2437) < 570


def test_bounding_box_contains_radius():
    min_lat, max_lat, min_lng, max_lng = bounding_box(0, 0, 10)
    assert haversine_km(0, 0, max_lat, 0) == pytest.approx(10, rel=1e-3)
    assert haversine_km(0, 0, 0, max_lng) == pytest.approx(10, rel=1e-3)
    assert min_lat < 0 < max_lat and min_lng < 0 < max_lng


def test_redaction_of_tokens_and_passwords():
    text = 'Authorization: Bearer eyJhbGciOi.eyJzdWIi.sig password=hunter2 {"refresh": "abc"}'
    out = redact(text)
    assert "hunter2" not in out and "eyJhbGciOi" not in out and '"abc"' not in out


def test_json_formatter_includes_extra_and_redacts():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token=abc123", None, None)
    record.driver_id = 7
    line = JsonFormatter().format(record)
    assert '"driver_id": 7' in line and "abc123" not in line
