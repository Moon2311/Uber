"""Step 10: correlation ids, metrics, security hardening and graceful degradation."""

import os
import subprocess
import sys

import pytest
from django.db import OperationalError
from django.test import override_settings
from django.urls import reverse
from prometheus_client import CollectorRegistry, generate_latest

from apps.common.metrics import InMemoryMetrics, PrometheusMetrics
from apps.events.consumer import ConsumedMessage, EventProcessor
from apps.events.models import OutboxEvent
from apps.events.relay import _to_message
from apps.events.tests.fakes import FakePublisher
from conftest import PASSWORD, client_for, make_driver, make_rider

pytestmark = pytest.mark.django_db
TRIP = {"pickup": {"lat": "37.7749", "lng": "-122.4194"},
        "dropoff": {"lat": "37.8044", "lng": "-122.2712"}, "ride_category": "ECONOMY"}  # fmt: skip


# --- correlation ids --------------------------------------------------------------------------
def test_request_id_is_generated_echoed_and_sanitized(api_client):
    generated = api_client.get("/api/v1/health/live/")["X-Request-ID"]
    assert len(generated) == 32
    assert api_client.get("/api/v1/health/live/", HTTP_X_REQUEST_ID="client-req-123")[
        "X-Request-ID"] == "client-req-123"  # fmt: skip
    hostile = "x\nFAKE LOG LINE level=ERROR" + "y" * 100
    assert api_client.get("/api/v1/health/live/", HTTP_X_REQUEST_ID=hostile)[
        "X-Request-ID"] != hostile  # fmt: skip


def test_request_id_follows_the_ride_through_outbox_kafka_and_consumers(rider_client):
    rider_client.post(reverse("v1:trip-create"), TRIP, format="json",
                      HTTP_X_REQUEST_ID="ride-req-0001")  # fmt: skip
    row = OutboxEvent.objects.get(event_type="RideRequested")
    assert row.correlation_id == "ride-req-0001"
    message = _to_message(row)
    assert ("correlation_id", b"ride-req-0001") in message.headers

    seen = []

    def handler(event):
        from apps.common.context import get_request_id

        seen.append(get_request_id())

    processor = EventProcessor("corr-test", {"RideRequested": handler}, FakePublisher())
    processor.process(ConsumedMessage("t", 0, 0, row.key, message.value, message.headers))
    assert seen == ["ride-req-0001"]


def test_log_records_carry_the_request_id():
    import logging

    from apps.common.context import bound_request_id
    from apps.common.logging import JsonFormatter, RequestIdFilter

    record = logging.LogRecord("t", logging.INFO, "", 0, "hello", (), None)
    with bound_request_id("abc12345"):
        RequestIdFilter().filter(record)
    assert '"request_id": "abc12345"' in JsonFormatter().format(record)


# --- metrics --------------------------------------------------------------------------------------
def test_prometheus_backend_exposition_and_label_safety():
    registry = CollectorRegistry()
    m = PrometheusMetrics(registry=registry)
    m.increment("matching.offer.created", tags={"eta_source": "osrm"})
    m.increment("matching.offer.created", tags={"eta_source": "fallback", "surprise": "x"})
    m.timing("matching.round.latency_ms", 42, tags={"outcome": "offered"})
    m.gauge("pricing.surge.multiplier", 1.5, tags={"zone": "872830828ffffff"})
    m.gauge_add("realtime.connections", 1, tags={"role": "RIDER"})
    m.gauge_add("realtime.connections", 1, tags={"role": "RIDER"})
    m.gauge_add("realtime.connections", -1, tags={"role": "RIDER"})
    text = generate_latest(registry).decode()
    assert 'rh_matching_offer_created_total{eta_source="osrm"} 1.0' in text
    assert "surprise" not in text  # labels are fixed at first use
    assert 'rh_matching_round_latency_ms_bucket{le="50.0",outcome="offered"} 1.0' in text
    assert "872830828ffffff" not in text and "rh_pricing_surge_multiplier 1.5" in text
    assert 'rh_realtime_connections_inflight{role="RIDER"} 1.0' in text


def test_metrics_endpoint_requires_the_scrape_token(api_client, settings):
    settings.METRICS_TOKEN = "scrape-token-123"
    assert api_client.get("/metrics").status_code == 403
    assert api_client.get("/metrics", HTTP_AUTHORIZATION="Bearer wrong").status_code == 403
    ok = api_client.get("/metrics", HTTP_AUTHORIZATION="Bearer scrape-token-123")
    assert ok.status_code == 200 and ok["Content-Type"].startswith("text/plain")
    settings.METRICS_TOKEN, settings.DEBUG = "", False
    assert api_client.get("/metrics").status_code == 403  # never open in production


def test_api_latency_is_recorded_per_route_template(rider_client, recorded_metrics):
    trip_id = rider_client.post(reverse("v1:trip-create"), TRIP, format="json").json()["id"]
    rider_client.get(f"/api/v1/trips/{trip_id}/")
    routes = {dict(tags).get("route") for (name, tags) in recorded_metrics.timings
              if name == "http.request.latency_ms"}  # fmt: skip
    assert "/api/v1/trips/<uuid:trip_id>/" in routes and trip_id not in str(routes)


def test_ops_monitor_isolates_failing_probes(monkeypatch, recorded_metrics):
    from apps.common import ops_metrics

    def broken(now):
        raise RuntimeError("kafka down")

    monkeypatch.setitem(ops_metrics.PROBES, "events", broken)
    make_driver(online=True)
    values = ops_metrics.collect()
    assert values["drivers.online"] == 1 and values["drivers.online_without_fresh_location"] == 1
    assert values["db.connections"] >= 1 and "events.outbox.pending" not in values
    assert recorded_metrics.count("ops.probe_failed", probe="events") == 1


# --- graceful degradation -------------------------------------------------------------------------
def test_database_errors_become_retryable_503(rider_client, monkeypatch):
    def down(**kw):
        raise OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr("apps.trips.views.request_trip", down)
    response = rider_client.post(reverse("v1:trip-create"), TRIP, format="json")
    assert response.status_code == 503 and response["Retry-After"] == "2"
    assert response.json()["error"]["code"] == "service_unavailable"


@pytest.mark.django_db(transaction=True)
def test_lock_timeout_bounds_waiting_for_a_busy_trip():
    """A request waiting on a row lock gives up (503) instead of pinning a worker."""
    import threading
    import time

    from django.db import connection, transaction

    from apps.trips.models import Trip
    from apps.trips.tests.conftest import new_trip

    trip = new_trip()
    locked, release = threading.Event(), threading.Event()

    def hold():
        with transaction.atomic():
            Trip.objects.select_for_update().get(pk=trip.pk)
            locked.set()
            release.wait(10)
        connection.close()

    holder = threading.Thread(target=hold)
    holder.start()
    locked.wait(5)
    with connection.cursor() as c:
        c.execute("SET lock_timeout = '300ms'")
    start = time.monotonic()
    response = client_for(trip.rider).post(f"/api/v1/trips/{trip.pk}/cancel/", {})
    elapsed = time.monotonic() - start
    release.set()
    holder.join()
    assert response.status_code == 503 and elapsed < 3
    trip.refresh_from_db()
    assert trip.status == "REQUESTED"  # nothing half-applied


def test_connection_has_bounded_timeouts():
    from django.db import connection

    with connection.cursor() as c:
        c.execute("SHOW statement_timeout")
        statement = c.fetchone()[0]
        c.execute("SHOW lock_timeout")
        lock = c.fetchone()[0]
    assert statement == "15s" and lock == "5s"


def test_rate_limiting_fails_open_when_redis_is_down(api_client, recorded_metrics):
    dead = {
        "default": {
            "BACKEND": "apps.common.cache.ResilientRedisCache",
            "LOCATION": "redis://localhost:1/0",
            "OPTIONS": {"socket_timeout": 0.1, "socket_connect_timeout": 0.1},
        }
    }
    with override_settings(CACHES=dead):
        response = api_client.post(
            reverse("v1:auth-login"),
            {"email": "nobody@example.com", "password": "x"},
            format="json",
        )
    assert response.status_code == 400  # handled normally, not a 500
    assert recorded_metrics.count("cache.unavailable", op="get") >= 1


def test_ready_probe_fails_without_database_but_live_does_not(api_client, monkeypatch):
    from django.db import connection

    def broken(*a, **kw):
        raise OperationalError("db down")

    monkeypatch.setattr(connection, "cursor", broken)
    assert api_client.get("/api/v1/health/ready/").status_code == 503
    assert api_client.get("/api/v1/health/live/").status_code == 200


# --- authentication -------------------------------------------------------------------------------
def test_refresh_tokens_rotate_and_logout_revokes(api_client):
    rider = make_rider()
    login = api_client.post(reverse("v1:auth-login"), {"email": rider.email, "password": PASSWORD},
                            format="json").json()  # fmt: skip
    first = login["refresh"]
    rotated = api_client.post(reverse("v1:auth-refresh"), {"refresh": first}, format="json")
    assert rotated.status_code == 200 and rotated.json()["refresh"] != first
    reuse = api_client.post(reverse("v1:auth-refresh"), {"refresh": first}, format="json")
    assert reuse.status_code == 401  # a stolen, already-used refresh token is dead

    current = rotated.json()["refresh"]
    client = client_for(rider)
    other = make_rider()
    from rest_framework_simplejwt.tokens import RefreshToken

    assert (
        client.post(
            reverse("v1:auth-logout"), {"refresh": str(RefreshToken.for_user(other))}, format="json"
        ).status_code
        == 400
    )  # cannot revoke someone else's
    assert client.post(reverse("v1:auth-logout"), {"refresh": current},
                       format="json").status_code == 205  # fmt: skip
    assert api_client.post(reverse("v1:auth-refresh"), {"refresh": current},
                           format="json").status_code == 401  # fmt: skip


def _rates(monkeypatch, **rates):
    from rest_framework.throttling import SimpleRateThrottle

    # DRF copies the rates onto the class at import time.
    monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES",
                        {**SimpleRateThrottle.THROTTLE_RATES, **rates})  # fmt: skip


def test_login_attempts_are_limited_per_account_across_ips(api_client, settings, monkeypatch):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
    _rates(monkeypatch, auth="100/min", anon="100/min", auth_email="3/hour")
    rider = make_rider()
    codes = [
        api_client.post(reverse("v1:auth-login"), {"email": rider.email, "password": "bad"},
                        format="json", HTTP_X_FORWARDED_FOR=f"203.0.113.{i}").status_code
        for i in range(5)
    ]  # fmt: skip
    assert codes == [400, 400, 400, 429, 429]


def test_client_ip_comes_from_the_trusted_proxy_hop(api_client, settings, monkeypatch):
    """With NUM_PROXIES=1, each real client gets its own bucket; without it every
    request behind the load balancer would share the LB's address."""
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
    _rates(monkeypatch, auth="2/min", anon="100/min", auth_email="100/hour")

    def login(ip, n):
        return api_client.post(reverse("v1:auth-login"), {"email": f"u{n}@x.io", "password": "x"},
                               format="json", HTTP_X_FORWARDED_FOR=ip).status_code  # fmt: skip

    a = [login("198.51.100.1", n) for n in range(3)]
    b = login("198.51.100.2", 9)
    assert a == [400, 400, 429] and b == 400


# --- authorization / privacy ----------------------------------------------------------------------
def test_nearby_drivers_cannot_be_tracked_across_riders(settings):
    from conftest import seed_location

    driver = make_driver(online=True)
    seed_location(driver, "37.774912", "-122.419488")
    a, b = client_for(make_rider()), client_for(make_rider())
    params = {"lat": 37.7749, "lng": -122.4194}
    ra = a.get("/api/v1/drivers/nearby/", params).json()["results"][0]
    rb = b.get("/api/v1/drivers/nearby/", params).json()["results"][0]
    assert ra["id"] != rb["id"] and str(driver.public_id) not in (ra["id"], rb["id"])
    assert ra["id"] == a.get("/api/v1/drivers/nearby/", params).json()["results"][0]["id"]
    assert (ra["lat"], ra["lng"]) == (37.775, -122.419)  # ~100 m, not the exact fix


def test_webhook_unknown_or_disabled_provider_is_404_and_oversized_is_413(api_client, settings):
    url = "/api/v1/payments/webhooks/{}/"
    assert api_client.post(url.format("stripe"), b"{}", content_type="application/json"
                           ).status_code == 404  # fmt: skip
    settings.PAYMENTS_SANDBOX_ALLOWED = False
    assert api_client.post(url.format("sandbox"), b"{}", content_type="application/json"
                           ).status_code == 404  # fmt: skip
    settings.PAYMENTS_SANDBOX_ALLOWED = True
    big = b'{"x": "' + b"a" * 70_000 + b'"}'
    assert api_client.post(url.format("sandbox"), big, content_type="application/json"
                           ).status_code == 413  # fmt: skip


def test_oversized_json_bodies_are_refused(rider_client):
    huge = {**TRIP, "pickup": {**TRIP["pickup"], "address": "x" * 300_000}}
    assert rider_client.post(reverse("v1:trip-create"), huge, format="json").status_code == 400


# --- configuration safety -------------------------------------------------------------------------
def _settings_subprocess(**env):
    base = {
        "PATH": os.environ["PATH"],
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DJANGO_SECRET_KEY": "x" * 60,
        "DATABASE_URL": "postgres://u:p@localhost:1/db",
    }
    return subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        env={**base, **env}, capture_output=True, text=True, timeout=60,
    )  # fmt: skip


def test_production_refuses_debug():
    result = _settings_subprocess(APP_ENV="production", DJANGO_DEBUG="true")
    assert result.returncode != 0 and "DJANGO_DEBUG must be false" in result.stderr


def test_deploy_checks_flag_unsafe_configuration(settings):
    from apps.common.checks import deployment_checks

    settings.APP_ENV = "production"
    settings.DEBUG = False
    settings.METRICS_TOKEN = ""
    settings.PAYMENTS_SANDBOX_ALLOWED = False
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    ids = {m.id for m in deployment_checks(None)}
    for expected in ("rh.E004", "rh.E007", "rh.E008", "rh.E010", "rh.E013"):
        assert expected in ids, (expected, ids)


def test_debug_dev_settings_still_work_for_local_development():
    result = _settings_subprocess(APP_ENV="development", DJANGO_DEBUG="true")
    assert result.returncode == 0, result.stderr


def test_inmemory_backend_supports_up_down_gauges():
    m = InMemoryMetrics()
    m.gauge_add("x", 2)
    m.gauge_add("x", -1)
    assert m.gauges[("x", ())] == 1
