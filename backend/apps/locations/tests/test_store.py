"""Contract tests: every LocationStore implementation must pass all of these."""

import random
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.utils import timezone

from apps.drivers.models import DriverProfile
from apps.locations.failover import FailoverLocationStore
from apps.locations.stores import (
    BoundingBox,
    HistoryPoint,
    LocationFix,
    PostgresLocationStore,
    WriteStatus,
)
from conftest import make_driver

pytestmark = pytest.mark.django_db


def _redis_store():
    from apps.locations.redis_store import RedisLocationStore

    return RedisLocationStore()


def _failover_store():
    return FailoverLocationStore(_redis_store(), PostgresLocationStore(), cooldown_seconds=5)


@pytest.fixture(params=["postgres", "redis", "failover"])
def store(request):
    if request.param == "postgres":
        return PostgresLocationStore()
    request.getfixturevalue("redis_client")
    return _redis_store() if request.param == "redis" else _failover_store()


def online(store, driver=None, category="ECONOMY"):
    driver = driver or make_driver(online=True)
    store.set_availability(driver.pk, online=True, category=category)
    return driver


def fix(driver_id, lat="1.0", lng="2.0", seconds_ago=0, cell=None):
    return LocationFix(
        driver_id=driver_id,
        latitude=Decimal(lat),
        longitude=Decimal(lng),
        recorded_at=timezone.now() - timedelta(seconds=seconds_ago),
        h3_cell=cell,
        h3_resolution=8 if cell else None,
    )


def test_insert_then_get_roundtrips_all_fields(store):
    d = online(store)
    f = LocationFix(
        driver_id=d.pk,
        latitude=Decimal("37.774929"),
        longitude=Decimal("-122.419416"),
        recorded_at=timezone.now().replace(microsecond=123456),
        heading=90.5,
        speed_kmh=12.25,
        accuracy_m=4.0,
        h3_cell="8828308281fffff",
        h3_resolution=8,
    )
    result = store.upsert_if_newer(f)
    assert result.status is WriteStatus.APPLIED and result.current.updated_at is not None
    got = store.get(d.pk)
    assert (got.latitude, got.longitude) == (Decimal("37.774929"), Decimal("-122.419416"))
    assert got.recorded_at == f.recorded_at  # microsecond precision preserved
    assert (got.heading, got.speed_kmh, got.accuracy_m) == (90.5, 12.25, 4.0)
    assert (got.h3_cell, got.h3_resolution) == ("8828308281fffff", 8)


def test_optional_fields_roundtrip_as_none(store):
    d = online(store)
    store.upsert_if_newer(fix(d.pk))
    got = store.get(d.pk)
    assert got.heading is None and got.speed_kmh is None and got.h3_cell is None


def test_older_and_equal_fixes_never_overwrite(store):
    d = online(store)
    newest = fix(d.pk, lat="5.0", seconds_ago=1)
    store.upsert_if_newer(newest)
    result = store.upsert_if_newer(fix(d.pk, lat="6.0", seconds_ago=10))
    assert result.status is WriteStatus.NOT_NEWER and result.current.latitude == Decimal("5.0")
    same_ts = LocationFix(**{**newest.__dict__, "latitude": Decimal("7.0")})
    assert store.upsert_if_newer(same_ts).status is WriteStatus.NOT_NEWER  # idempotent
    assert store.get(d.pk).latitude == Decimal("5.0")


def test_offline_driver_rejected(store):
    d = make_driver(online=False)
    store.set_availability(d.pk, online=False, category=None)
    assert store.upsert_if_newer(fix(d.pk)).status is WriteStatus.DRIVER_OFFLINE
    assert store.get(d.pk) is None


def test_going_offline_removes_location_and_index(store):
    d = online(store)
    store.upsert_if_newer(fix(d.pk, cell="8828308281fffff"))
    DriverProfile.objects.filter(pk=d.pk).update(availability_status="OFFLINE")
    store.set_availability(d.pk, online=False, category=None)
    assert store.get(d.pk) is None
    assert store.find_in_cells(["8828308281fffff"], fresh_after=timezone.now() - timedelta(1)) == []


def test_remove_is_idempotent(store):
    d = online(store)
    store.upsert_if_newer(fix(d.pk))
    store.remove(d.pk)
    assert store.get(d.pk) is None
    store.remove(d.pk)


def test_find_in_bbox_filters_area_and_freshness(store):
    inside, outside, stale = online(store), online(store), online(store)
    store.upsert_if_newer(fix(inside.pk, lat="1.0"))
    store.upsert_if_newer(fix(outside.pk, lat="9.0"))
    store.upsert_if_newer(fix(stale.pk, lat="1.0", seconds_ago=600))
    found = store.find_in_bbox(
        BoundingBox(0.5, 1.5, 1.5, 2.5), fresh_after=timezone.now() - timedelta(seconds=120)
    )
    assert [f.driver_id for f in found] == [inside.pk]


def test_find_in_cells_filters_cells_and_freshness(store):
    a, b, stale = online(store), online(store), online(store)
    store.upsert_if_newer(fix(a.pk, cell="8828308281fffff"))
    store.upsert_if_newer(fix(b.pk, cell="8828308283fffff"))
    store.upsert_if_newer(fix(stale.pk, cell="8828308281fffff", seconds_ago=600))
    fresh_after = timezone.now() - timedelta(seconds=120)
    assert [
        f.driver_id for f in store.find_in_cells(["8828308281fffff"], fresh_after=fresh_after)
    ] == [a.pk]
    assert store.find_in_cells([], fresh_after=fresh_after) == []


def test_history_marker(store):
    d = online(store)
    store.upsert_if_newer(fix(d.pk))
    point = HistoryPoint(Decimal("1.5"), Decimal("2.5"), timezone.now().replace(microsecond=0))
    store.mark_history_point(d.pk, point)
    if isinstance(store, PostgresLocationStore):
        assert store.last_history_point(d.pk) is None  # derived from event rows
    else:
        assert store.last_history_point(d.pk) == point


def test_prune_removes_only_old_fixes(store):
    old, new = online(store), online(store)
    store.upsert_if_newer(fix(old.pk, seconds_ago=3600))
    store.upsert_if_newer(fix(new.pk))
    assert store.prune(older_than=timezone.now() - timedelta(minutes=30)) == 1
    assert store.get(old.pk) is None and store.get(new.pk) is not None


def test_health(store):
    assert store.health()["status"] == "ok"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["postgres", "redis"])
def test_concurrent_out_of_order_writes_keep_newest(kind, request):
    """Writers racing in separate connections must converge on the newest fix, and the
    driver must end up a member of exactly one cell (the stored one)."""
    import h3

    if kind == "redis":
        request.getfixturevalue("redis_client")
    make = PostgresLocationStore if kind == "postgres" else _redis_store
    d = make_driver(online=True)
    make().set_availability(d.pk, online=True, category="ECONOMY")
    base = timezone.now()
    cells = list(h3.grid_disk("8828308281fffff", 2))
    fixes = [
        LocationFix(
            driver_id=d.pk,
            latitude=Decimal(i),
            longitude=Decimal("2.0"),
            recorded_at=base - timedelta(seconds=60 - i),
            h3_cell=cells[i % len(cells)],
            h3_resolution=8,
        )
        for i in range(1, 41)
    ]
    random.shuffle(fixes)

    def write(f):
        try:
            return make().upsert_if_newer(f).applied
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, fixes))
    final = make().get(d.pk)
    assert final.latitude == Decimal(40) and final.h3_cell == cells[40 % len(cells)]
    fresh_after = base - timedelta(minutes=5)
    members = [c for c in cells if make().find_in_cells([c], fresh_after=fresh_after)]
    assert members == [final.h3_cell]
    if kind == "redis":
        # raw index check (find_in_cells would hide a leaked membership)
        store = make()
        raw = [c for c in cells if store.r.zscore(store.k_cell(c), d.pk) is not None]
        assert raw == [final.h3_cell]
        assert store.r.hget(store.k_lastcell, d.pk) == final.h3_cell
