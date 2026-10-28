"""Redis hot store specifics: key layout, cell transitions, TTL/expiry, idempotency,
Lua atomicity, reconnects and failure translation."""

import time
from datetime import timedelta
from decimal import Decimal

import h3
import pytest
import redis
from django.utils import timezone

from apps.locations.redis_store import RedisLocationStore
from apps.locations.stores import LocationFix, StoreUnavailable, WriteStatus
from conftest import TEST_REDIS_URL, make_driver

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("redis_client")]

A = "8828308281fffff"
B, C = [c for c in h3.grid_ring(A, 1)][:2]


@pytest.fixture
def store():
    return RedisLocationStore()


@pytest.fixture
def driver(store):
    d = make_driver(online=True)
    store.set_availability(d.pk, online=True, category="XL")
    return d


def fix(driver_id, cell, seconds_ago=0, lat="37.7749"):
    return LocationFix(
        driver_id=driver_id,
        latitude=Decimal(lat),
        longitude=Decimal("-122.4194"),
        recorded_at=timezone.now() - timedelta(seconds=seconds_ago),
        h3_cell=cell,
        h3_resolution=8 if cell else None,
    )


def members(store, r, cell):
    return set(r.zrange(store.k_cell(cell), 0, -1))


# --- key layout ------------------------------------------------------------------
def test_all_keys_share_one_cluster_hash_slot(store, driver, redis_client):
    store.upsert_if_newer(fix(driver.pk, A))
    keys = list(redis_client.scan_iter(match=f"{store.key_prefix}:*"))
    assert len(keys) >= 5 and all("{live}" in k for k in keys)


def test_driver_hash_contents_and_ttl(store, driver, redis_client, settings):
    store.upsert_if_newer(fix(driver.pk, A))
    h = redis_client.hgetall(store.k_drv(driver.pk))
    assert h["status"] == "ONLINE" and h["category"] == "XL" and h["cell"] == A
    ttl = redis_client.ttl(store.k_drv(driver.pk))
    assert 0 < ttl <= settings.LIVE_DRIVER_TTL_SECONDS
    assert 0 < redis_client.ttl(store.k_cell(A)) <= settings.LIVE_DRIVER_TTL_SECONDS


# --- cell transitions ---------------------------------------------------------------
def test_move_updates_old_and_new_cell_atomically(store, driver, redis_client):
    store.upsert_if_newer(fix(driver.pk, A, seconds_ago=10))
    assert members(store, redis_client, A) == {str(driver.pk)}
    store.upsert_if_newer(fix(driver.pk, B, seconds_ago=5))
    assert members(store, redis_client, A) == set()
    assert members(store, redis_client, B) == {str(driver.pk)}
    assert redis_client.hget(store.k_lastcell, driver.pk) == B


def test_out_of_order_fix_does_not_move_driver_back(store, driver, redis_client):
    store.upsert_if_newer(fix(driver.pk, B, seconds_ago=1))
    assert store.upsert_if_newer(fix(driver.pk, A, seconds_ago=30)).status is WriteStatus.NOT_NEWER
    assert members(store, redis_client, A) == set()
    assert members(store, redis_client, B) == {str(driver.pk)}


def test_same_cell_update_keeps_single_membership(store, driver, redis_client):
    for s in (30, 20, 10):
        store.upsert_if_newer(fix(driver.pk, A, seconds_ago=s))
    assert members(store, redis_client, A) == {str(driver.pk)}
    assert redis_client.zscore(store.k_cell(A), driver.pk) == pytest.approx(
        int(store.get(driver.pk).recorded_at.timestamp() * 1e6), abs=1
    )


def test_move_to_no_cell_and_back(store, driver, redis_client):
    store.upsert_if_newer(fix(driver.pk, A, seconds_ago=10))
    store.upsert_if_newer(fix(driver.pk, None, seconds_ago=5))  # indexing failed
    assert members(store, redis_client, A) == set()
    assert redis_client.hget(store.k_lastcell, driver.pk) is None
    store.upsert_if_newer(fix(driver.pk, C, seconds_ago=1))
    assert members(store, redis_client, C) == {str(driver.pk)}


def test_concurrent_move_between_read_and_script_is_retried(
    store, driver, redis_client, monkeypatch, recorded_metrics
):
    """Simulate another writer moving the driver after we read its current cell: the
    Lua script detects the mismatch (RETRY) and the client re-reads."""
    store.upsert_if_newer(fix(driver.pk, A, seconds_ago=10))
    real_hget = store.r.hget
    calls = {"n": 0}

    def stale_hget(key, field):
        calls["n"] += 1
        if calls["n"] == 1 and field == "cell":
            return C  # stale view
        return real_hget(key, field)

    monkeypatch.setattr(store.r, "hget", stale_hget)
    assert store.upsert_if_newer(fix(driver.pk, B, seconds_ago=1)).applied
    assert recorded_metrics.count("location.store.cas_retry") == 1
    assert members(store, redis_client, A) == set()
    assert members(store, redis_client, B) == {str(driver.pk)}


def test_offline_clears_every_index(store, driver, redis_client):
    store.upsert_if_newer(fix(driver.pk, A))
    store.set_availability(driver.pk, online=False, category=None)
    assert members(store, redis_client, A) == set()
    assert redis_client.zscore(store.k_seen, driver.pk) is None
    assert redis_client.geopos(store.k_geo, driver.pk) == [None]
    assert redis_client.hget(store.k_lastcell, driver.pk) is None
    assert redis_client.hgetall(store.k_drv(driver.pk)) == {"status": "OFFLINE", "category": ""}
    # a late fix after going offline is rejected, nothing is resurrected
    assert store.upsert_if_newer(fix(driver.pk, A)).status is WriteStatus.DRIVER_OFFLINE
    assert members(store, redis_client, A) == set()


# --- idempotency ------------------------------------------------------------------------
def test_repeated_identical_update_is_a_noop(store, driver, redis_client):
    f = fix(driver.pk, A)
    assert store.upsert_if_newer(f).applied
    snapshot = redis_client.hgetall(store.k_drv(driver.pk))
    for _ in range(3):
        assert store.upsert_if_newer(f).status is WriteStatus.NOT_NEWER
    assert redis_client.hgetall(store.k_drv(driver.pk)) == snapshot


def test_availability_changes_are_idempotent(store, driver, redis_client):
    store.upsert_if_newer(fix(driver.pk, A))
    store.set_availability(driver.pk, online=True, category="XL")
    assert store.get(driver.pk) is not None  # going online again keeps the fix
    store.set_availability(driver.pk, online=False, category=None)
    store.set_availability(driver.pk, online=False, category=None)
    assert store.get(driver.pk) is None


def test_seed_never_overrides_existing_status(store, driver, redis_client):
    store.set_availability(driver.pk, online=False, category=None)
    store.seed_availability(driver.pk, online=True, category="XL")
    assert redis_client.hget(store.k_drv(driver.pk), "status") == "OFFLINE"
    redis_client.delete(store.k_drv(driver.pk))
    store.seed_availability(driver.pk, online=True, category="XL")
    assert redis_client.hget(store.k_drv(driver.pk), "status") == "ONLINE"


# --- expiry / staleness -----------------------------------------------------------------
def test_unknown_status_after_key_loss(store, driver, redis_client):
    redis_client.delete(store.k_drv(driver.pk))
    assert store.upsert_if_newer(fix(driver.pk, A)).status is WriteStatus.DRIVER_UNKNOWN


def test_expired_driver_hash_is_never_returned_even_if_index_remains(store, driver, redis_client):
    store.upsert_if_newer(fix(driver.pk, A))
    redis_client.pexpire(store.k_drv(driver.pk), 1)
    time.sleep(0.05)
    assert members(store, redis_client, A) == {str(driver.pk)}  # index not yet pruned
    assert store.find_in_cells([A], fresh_after=timezone.now() - timedelta(minutes=5)) == []
    assert store.get(driver.pk) is None


def test_stale_fix_excluded_by_timestamp_before_ttl(store, driver):
    store.upsert_if_newer(fix(driver.pk, A, seconds_ago=200))
    fresh_after = timezone.now() - timedelta(seconds=120)
    assert store.find_in_cells([A], fresh_after=fresh_after) == []
    assert store.get(driver.pk) is not None  # still readable (STALE, not MISSING)


def test_index_entry_for_moved_driver_ignored(store, driver, redis_client):
    store.upsert_if_newer(fix(driver.pk, B))
    redis_client.zadd(store.k_cell(A), {str(driver.pk): time.time() * 1e6})  # leftover
    fresh_after = timezone.now() - timedelta(minutes=1)
    assert store.find_in_cells([A], fresh_after=fresh_after) == []
    assert [f.driver_id for f in store.find_in_cells([A, B], fresh_after=fresh_after)] == [
        driver.pk
    ]


def test_prune_cleans_index_after_hash_expired(store, driver, redis_client):
    store.upsert_if_newer(fix(driver.pk, A, seconds_ago=3600))
    redis_client.delete(store.k_drv(driver.pk))  # TTL already reclaimed the hash
    assert store.prune(older_than=timezone.now() - timedelta(minutes=30)) == 1
    assert members(store, redis_client, A) == set()
    assert redis_client.zscore(store.k_seen, driver.pk) is None
    assert redis_client.hget(store.k_lastcell, driver.pk) is None


def test_prune_skips_driver_who_reported_again(store, driver, redis_client):
    store.upsert_if_newer(fix(driver.pk, A))
    assert store.prune(older_than=timezone.now() - timedelta(minutes=30)) == 0
    assert members(store, redis_client, A) == {str(driver.pk)}


def test_high_latitude_fix_stored_without_geo_entry(store, driver, redis_client):
    f = LocationFix(
        driver_id=driver.pk,
        latitude=Decimal("89.5"),
        longitude=Decimal("10"),
        recorded_at=timezone.now(),
        h3_cell=h3.latlng_to_cell(89.5, 10, 8),
        h3_resolution=8,
    )
    assert store.upsert_if_newer(f).applied  # GEOADD would reject |lat| > 85.05
    assert redis_client.geopos(store.k_geo, driver.pk) == [None]


def test_reindex_moves_cells(store, driver, redis_client):
    store.upsert_if_newer(fix(driver.pk, h3.latlng_to_cell(37.7749, -122.4194, 8)))
    updated, failed = store.reindex_cells(lambda lat, lng: (h3.latlng_to_cell(lat, lng, 9), 9))
    assert (updated, failed) == (1, 0)
    new_cell = h3.latlng_to_cell(37.7749, -122.4194, 9)
    assert store.get(driver.pk).h3_cell == new_cell
    assert members(store, redis_client, new_cell) == {str(driver.pk)}


# --- failures / reconnect ----------------------------------------------------------------
def test_connection_errors_become_store_unavailable():
    dead = redis.Redis.from_url(
        "redis://localhost:1/0", socket_connect_timeout=0.1, decode_responses=True
    )
    store = RedisLocationStore(client=dead)
    with pytest.raises(StoreUnavailable):
        store.get(1)
    with pytest.raises(StoreUnavailable):
        store.upsert_if_newer(fix(1, A))
    assert store.health()["status"] == "unavailable"


def test_non_connectivity_errors_are_not_masked(store, monkeypatch):
    def broken(*args, **kwargs):
        raise redis.exceptions.ResponseError("ERR Error running script: bug")

    monkeypatch.setattr(store, "_upsert", broken)
    with pytest.raises(redis.exceptions.ResponseError):
        store.upsert_if_newer(fix(1, A))


def test_oom_is_treated_as_unavailable(store, monkeypatch):
    def oom(*args, **kwargs):
        raise redis.exceptions.ResponseError(
            "OOM command not allowed when used memory > 'maxmemory'."
        )

    monkeypatch.setattr(store, "_upsert", oom)
    with pytest.raises(StoreUnavailable):
        store.upsert_if_newer(fix(1, A))


def test_reconnects_after_server_kills_connections(store, driver, redis_client):
    store.upsert_if_newer(fix(driver.pk, A, seconds_ago=5))
    admin = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    ours = [
        c["id"]
        for c in admin.client_list()
        if c.get("name") == "ride-hailing-backend" and c.get("db") == "15"
    ]
    assert ours
    for client_id in ours:
        admin.client_kill_filter(_id=client_id)
    # pooled connection is dead; redis-py retries on a fresh connection
    assert store.upsert_if_newer(fix(driver.pk, B)).applied
    assert store.get(driver.pk).h3_cell == B


def test_survives_script_cache_flush(store, driver, redis_client):
    redis_client.script_flush()  # e.g. Redis restarted: EVALSHA -> NOSCRIPT
    assert store.upsert_if_newer(fix(driver.pk, A)).applied
