"""Contract tests for LocationStore implementations (reuse for the Redis store)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.locations.stores import BoundingBox, LocationFix, PostgresLocationStore
from conftest import make_driver

pytestmark = pytest.mark.django_db


@pytest.fixture(params=[PostgresLocationStore])
def store(request):
    return request.param()


def fix(driver_id, lat="1.0", seconds_ago=0):
    return LocationFix(
        driver_id=driver_id,
        latitude=Decimal(lat),
        longitude=Decimal("2.0"),
        recorded_at=timezone.now() - timedelta(seconds=seconds_ago),
    )


def test_insert_then_get(store):
    d = make_driver()
    applied, current = store.upsert_if_newer(fix(d.pk))
    assert applied and current.updated_at is not None
    assert store.get(d.pk).latitude == Decimal("1.0")


def test_older_and_equal_fixes_never_overwrite(store):
    d = make_driver()
    newest = fix(d.pk, lat="5.0", seconds_ago=1)
    store.upsert_if_newer(newest)
    applied, current = store.upsert_if_newer(fix(d.pk, lat="6.0", seconds_ago=10))
    assert not applied and current.latitude == Decimal("5.0")
    same_ts = LocationFix(**{**newest.__dict__, "latitude": Decimal("7.0")})
    assert store.upsert_if_newer(same_ts)[0] is False
    assert store.get(d.pk).latitude == Decimal("5.0")


def test_remove(store):
    d = make_driver()
    store.upsert_if_newer(fix(d.pk))
    store.remove(d.pk)
    assert store.get(d.pk) is None
    store.remove(d.pk)  # idempotent


def test_find_in_bbox_filters_area_and_freshness(store):
    inside, outside, stale = make_driver(), make_driver(), make_driver()
    store.upsert_if_newer(fix(inside.pk, lat="1.0"))
    store.upsert_if_newer(fix(outside.pk, lat="9.0"))
    store.upsert_if_newer(fix(stale.pk, lat="1.0", seconds_ago=600))
    found = store.find_in_bbox(
        BoundingBox(0.5, 1.5, 1.5, 2.5), fresh_after=timezone.now() - timedelta(seconds=120)
    )
    assert [f.driver_id for f in found] == [inside.pk]


@pytest.mark.django_db(transaction=True)
def test_concurrent_out_of_order_writes_keep_newest():
    """Writers racing in separate DB connections must converge on the newest fix."""
    import random
    from concurrent.futures import ThreadPoolExecutor

    from django.db import connection

    d = make_driver()
    base = timezone.now()
    fixes = [
        LocationFix(
            driver_id=d.pk,
            latitude=Decimal(i),
            longitude=Decimal("2.0"),
            recorded_at=base - timedelta(seconds=60 - i),
        )
        for i in range(1, 41)
    ]
    random.shuffle(fixes)

    def write(f):
        try:
            return PostgresLocationStore().upsert_if_newer(f)[0]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, fixes))
    assert PostgresLocationStore().get(d.pk).latitude == Decimal(40)
