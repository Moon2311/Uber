"""Redis outage handling: circuit breaker, PostgreSQL fallback, recovery/replay, and
end-to-end API behaviour with Redis down."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.drivers.services import set_availability
from apps.locations.failover import FailoverLocationStore
from apps.locations.models import DriverLocation
from apps.locations.redis_store import RedisLocationStore
from apps.locations.service import build_store
from apps.locations.stores import LocationFix, PostgresLocationStore, StoreUnavailable
from conftest import client_for, make_driver

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("redis_client")]
CELL = "8828308281fffff"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FlakyRedis(RedisLocationStore):
    """Real Redis store whose availability can be toggled by the test."""

    down = False

    def _run(self, fn, *args, **kwargs):
        if self.down:
            raise StoreUnavailable("simulated outage")
        return super()._run(fn, *args, **kwargs)


@pytest.fixture
def setup():
    clock = Clock()
    primary = FlakyRedis()
    recoveries = []
    store = FailoverLocationStore(
        primary,
        PostgresLocationStore(),
        cooldown_seconds=5,
        on_recovery=recoveries.append,
        monotonic=clock,
    )
    return store, primary, clock, recoveries


def fix(driver_id, seconds_ago=0, lat="1.0"):
    return LocationFix(
        driver_id=driver_id,
        latitude=Decimal(lat),
        longitude=Decimal("2.0"),
        recorded_at=timezone.now() - timedelta(seconds=seconds_ago),
        h3_cell=CELL,
        h3_resolution=8,
    )


def fresh():
    return timezone.now() - timedelta(minutes=2)


def test_normal_operation_uses_redis_only(setup):
    store, primary, _, _ = setup
    d = make_driver(online=True)
    store.set_availability(d.pk, online=True, category="ECONOMY")
    assert store.upsert_if_newer(fix(d.pk)).applied
    assert primary.get(d.pk) is not None
    assert not DriverLocation.objects.exists()


def test_outage_falls_back_to_postgres_and_opens_circuit(setup, recorded_metrics):
    store, primary, clock, _ = setup
    d = make_driver(online=True)
    primary.down = True
    assert store.upsert_if_newer(fix(d.pk)).applied
    assert DriverLocation.objects.filter(driver=d).exists()
    assert store.is_open and store.degraded
    assert [f.driver_id for f in store.find_in_cells([CELL], fresh_after=fresh())] == [d.pk]
    assert recorded_metrics.count("location.store.failover", op="upsert_if_newer") == 1
    # circuit open: Redis is not even attempted
    primary.down = False
    store.get(d.pk)
    assert recorded_metrics.count("location.store.fallback_call", op="get") == 1


def test_recovery_resyncs_replays_and_purges_fallback(setup, recorded_metrics):
    store, primary, clock, recoveries = setup
    d = make_driver(online=True)
    store.set_availability(d.pk, online=True, category="ECONOMY")
    store.upsert_if_newer(fix(d.pk, seconds_ago=30, lat="1.0"))
    primary.down = True
    store.upsert_if_newer(fix(d.pk, seconds_ago=5, lat="3.0"))  # lands in PostgreSQL
    primary.down = False
    clock.t += 6  # cooldown elapsed -> half-open
    got = store.get(d.pk)  # first successful primary call triggers recovery
    assert len(recoveries) == 1
    assert got is not None
    assert primary.get(d.pk).latitude == Decimal("3.0")  # replayed (newer wins)
    assert not DriverLocation.objects.exists()  # fallback purged
    assert not store.degraded and recorded_metrics.count("location.store.recovered") == 1


def test_replay_never_regresses_a_newer_redis_fix(setup):
    store, primary, clock, _ = setup
    d = make_driver(online=True)
    store.set_availability(d.pk, online=True, category="ECONOMY")
    primary.down = True
    store.upsert_if_newer(fix(d.pk, seconds_ago=20, lat="2.0"))  # fallback
    primary.down = False
    primary.upsert_if_newer(fix(d.pk, seconds_ago=1, lat="9.0"))  # another process wrote newer
    clock.t += 6
    store.get(d.pk)
    assert primary.get(d.pk).latitude == Decimal("9.0")


def test_failed_half_open_probe_reopens_circuit(setup):
    store, primary, clock, recoveries = setup
    d = make_driver(online=True)
    primary.down = True
    store.get(d.pk)
    clock.t += 6
    store.get(d.pk)  # probe fails
    assert store.is_open and recoveries == []


def test_offline_during_outage_clears_both_stores(setup):
    store, primary, clock, _ = setup
    d = make_driver(online=True)
    store.set_availability(d.pk, online=True, category="ECONOMY")
    store.upsert_if_newer(fix(d.pk, seconds_ago=10))  # in Redis
    primary.down = True
    store.upsert_if_newer(fix(d.pk))  # in PostgreSQL
    from apps.drivers.models import DriverProfile

    DriverProfile.objects.filter(pk=d.pk).update(availability_status="OFFLINE")
    store.set_availability(d.pk, online=False, category=None)
    assert not DriverLocation.objects.filter(driver=d).exists()


# --- through the real service / API --------------------------------------------------------
@pytest.fixture
def redis_with_fallback(settings):
    settings.LOCATION_STORE = "redis"
    settings.LOCATION_STORE_FALLBACK = "postgres"
    settings.LOCATION_STORE_CIRCUIT_COOLDOWN_SECONDS = 0
    return settings


def test_api_keeps_working_when_redis_is_unreachable(redis_with_fallback, settings):
    settings.REDIS_URL = "redis://localhost:1/0"  # nothing listens here
    driver = make_driver(online=True)
    response = client_for(driver.user).post(
        "/api/v1/locations/update/", {"lat": 37.7749, "lng": -122.4194}, format="json"
    )
    assert response.status_code == 200 and response.json()["accepted"] is True
    assert DriverLocation.objects.filter(driver=driver).exists()
    health = client_for(driver.user).get("/api/v1/health/").json()
    assert health["status"] == "degraded" and health["live_store"]["status"] == "degraded"


def test_api_returns_503_without_fallback(settings):
    settings.LOCATION_STORE = "redis"
    settings.LOCATION_STORE_FALLBACK = "none"
    settings.REDIS_URL = "redis://localhost:1/0"
    driver = make_driver(online=True)
    response = client_for(driver.user).post(
        "/api/v1/locations/update/", {"lat": 37.7749, "lng": -122.4194}, format="json"
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert client_for(driver.user).get("/api/v1/health/").status_code == 503


def test_health_reports_redis_ok(redis_with_fallback, api_client):
    body = api_client.get("/api/v1/health/").json()
    assert body["status"] == "ok"
    assert body["live_store"]["primary"]["backend"] == "redis"
    assert body["live_store"]["primary"]["status"] == "ok"


def test_redis_data_loss_self_heals_from_postgres(redis_with_fallback, redis_client, settings):
    driver = make_driver(online=True)
    client = client_for(driver.user)
    assert client.post("/api/v1/locations/update/", {"lat": 1, "lng": 2}, format="json").json()[
        "accepted"
    ]
    for key in redis_client.scan_iter(match=f"{settings.REDIS_KEY_PREFIX}:*"):
        redis_client.delete(key)  # FLUSH / restart without persistence
    # next ping: status unknown in Redis -> seeded from PostgreSQL -> applied
    assert client.post("/api/v1/locations/update/", {"lat": 1.1, "lng": 2}, format="json").json()[
        "accepted"
    ]
    assert isinstance(build_store(), FailoverLocationStore)


def test_offline_driver_is_not_seeded_online(redis_with_fallback):
    driver = make_driver(online=False)
    response = client_for(driver.user).post(
        "/api/v1/locations/update/", {"lat": 1, "lng": 2}, format="json"
    )
    assert response.status_code == 409


def test_availability_toggle_mirrors_into_redis(redis_with_fallback, redis_client, settings):
    driver = make_driver(online=False)
    set_availability(driver=driver, online=True)
    store = build_store().primary
    assert redis_client.hgetall(store.k_drv(driver.pk)) == {
        "status": "ONLINE",
        "category": "ECONOMY",
    }
    set_availability(driver=driver, online=False)
    assert redis_client.hget(store.k_drv(driver.pk), "status") == "OFFLINE"


def test_rebuild_and_prune_commands(redis_with_fallback, redis_client, settings):
    from io import StringIO

    from django.core.management import call_command

    online_driver = make_driver(online=True)
    out = StringIO()
    call_command("rebuild_live_index", stdout=out)
    assert "re-pushed" in out.getvalue()
    store = build_store().primary
    assert redis_client.hget(store.k_drv(online_driver.pk), "status") == "ONLINE"
    call_command("prune_live_index", stdout=out)
    assert "Pruned 0" in out.getvalue()


def test_offline_during_outage_is_resynced_on_recovery_end_to_end(redis_with_fallback, monkeypatch):
    """Real recovery path: a driver goes offline while Redis is down, Redis keeps a
    stale ONLINE mirror + fix; recovery re-pushes availability from PostgreSQL."""
    from apps.drivers.candidates import find_candidate_drivers

    store = build_store()
    driver = make_driver(online=False)
    set_availability(driver=driver, online=True)
    client = client_for(driver.user)
    client.post("/api/v1/locations/update/", {"lat": 37.7749, "lng": -122.4194}, format="json")
    assert store.primary.get(driver.pk) is not None

    real_run = RedisLocationStore._run

    def outage(self, fn, *args, **kwargs):
        raise StoreUnavailable("simulated outage")

    monkeypatch.setattr(RedisLocationStore, "_run", outage)
    set_availability(driver=driver, online=False)  # PostgreSQL OFFLINE; mirror write failed
    assert store.degraded
    monkeypatch.setattr(RedisLocationStore, "_run", real_run)
    assert store.primary.get(driver.pk) is not None  # stale ONLINE mirror still in Redis

    # Cooldown is 0: the next call probes Redis, succeeds and triggers recovery.
    result = find_candidate_drivers(lat=37.7749, lng=-122.4194, radius_km=2, limit=5)
    assert result == []
    assert not store.degraded
    assert store.primary.get(driver.pk) is None
    from apps.common.redis import get_redis

    assert get_redis().hget(store.primary.k_drv(driver.pk), "status") == "OFFLINE"


# --- process-independent reconcile ------------------------------------------------------
def test_reconcile_after_keyspace_loss_resyncs_everyone_and_replays(
    redis_with_fallback, redis_client, settings
):
    from io import StringIO

    from django.core.management import call_command

    online_driver, offline_driver = make_driver(online=True), make_driver(online=False)
    store = build_store()
    assert store.primary.ensure_epoch() is True  # first use of this keyspace
    assert store.reconcile()["action"] == "none"
    # A fix that another (degraded) worker wrote to the fallback during an outage:
    PostgresLocationStore().upsert_if_newer(fix(online_driver.pk))
    for key in redis_client.scan_iter(match=f"{settings.REDIS_KEY_PREFIX}:*"):
        redis_client.delete(key)  # Redis restarted empty
    out = StringIO()
    call_command("reconcile_live_store", stdout=out)
    assert "resync_all" in out.getvalue() and "replayed 1" in out.getvalue()
    k = store.primary.k_drv
    assert redis_client.hget(k(online_driver.pk), "status") == "ONLINE"
    assert redis_client.hget(k(offline_driver.pk), "status") == "OFFLINE"
    assert store.primary.get(online_driver.pk) is not None  # replayed
    assert not DriverLocation.objects.exists()  # fallback purged
    assert store.reconcile()["action"] == "none"  # idempotent


def test_reconcile_replays_pending_fallback_rows_without_keyspace_loss(redis_with_fallback):
    d = make_driver(online=True)
    store = build_store()
    store.primary.ensure_epoch()
    set_availability(driver=d, online=True)
    PostgresLocationStore().upsert_if_newer(fix(d.pk))
    result = store.reconcile()
    assert result == {"action": "replay", "replayed": 1}
    assert store.primary.get(d.pk) is not None and not DriverLocation.objects.exists()


def test_reconcile_command_fails_cleanly_when_redis_down(redis_with_fallback, settings):
    from django.core.management import CommandError, call_command

    settings.REDIS_URL = "redis://localhost:1/0"
    with pytest.raises(CommandError):
        call_command("reconcile_live_store")
