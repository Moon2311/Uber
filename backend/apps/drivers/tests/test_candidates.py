"""H3 candidate retrieval (step 3)."""

import math
import random
from io import StringIO

import h3
import pytest
from django.core.management import call_command

from apps.common.geo import haversine_km
from apps.drivers.candidates import find_candidate_drivers
from apps.drivers.models import DriverProfile
from apps.geoindex.service import get_geo_search_service
from apps.trips.models import Trip, TripStatus
from conftest import latest, make_driver, make_rider, seed_location

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("live_store_backend")]
SF = (37.7749, -122.4194)


def place(driver, lat, lng, seconds_ago=0, index=True):
    seed_location(driver, lat, lng, seconds_ago=seconds_ago, index=index)


def search(lat=SF[0], lng=SF[1], radius_km=5.0, limit=25, **kw):
    return find_candidate_drivers(lat=lat, lng=lng, radius_km=radius_km, limit=limit, **kw)


def ids(candidates):
    return [c.driver_id for c in candidates]


def straddle_cell_edge(lat, lng, offset_km=0.01):
    """Two points ~2*offset_km apart on opposite sides of a cell edge."""
    geo = get_geo_search_service()
    cell = geo.cell_for(lat, lng)
    clat, clng = h3.cell_to_latlng(cell)
    v1, v2 = h3.cell_to_boundary(cell)[:2]
    mlat, mlng = (v1[0] + v2[0]) / 2, (v1[1] + v2[1]) / 2
    # unit vector from center to edge midpoint, in degrees scaled per km
    dlat_km = (mlat - clat) * 111.32
    dlng_km = (mlng - clng) * 111.32 * math.cos(math.radians(clat))
    norm = math.hypot(dlat_km, dlng_km)
    ulat, ulng = dlat_km / norm, dlng_km / norm

    def at(sign):
        return (
            mlat + sign * offset_km * ulat / 111.32,
            mlng + sign * offset_km * ulng / (111.32 * math.cos(math.radians(clat))),
        )

    inside, outside = at(-1), at(+1)
    assert geo.cell_for(*inside) == cell != geo.cell_for(*outside)
    return inside, outside, cell


# --- boundaries ------------------------------------------------------------------
def test_driver_across_cell_boundary_is_found():
    pickup, driver_point, _ = straddle_cell_edge(*SF)
    d = make_driver(online=True)
    place(d, *driver_point)
    [c] = search(*pickup, radius_km=1)
    assert c.driver_id == d.pk and c.grid_distance == 1 and c.distance_km < 0.05


def test_nearest_driver_wins_even_if_in_neighbouring_cell():
    pickup, across, cell = straddle_cell_edge(*SF)
    far_same_cell = make_driver(online=True)
    near_other_cell = make_driver(online=True)
    # far side of the pickup's own cell (~2 edge lengths away)
    clat, clng = h3.cell_to_latlng(cell)
    place(far_same_cell, 2 * clat - pickup[0], 2 * clng - pickup[1])
    assert get_geo_search_service().cell_for(2 * clat - pickup[0], 2 * clng - pickup[1]) == cell
    place(near_other_cell, *across)
    assert ids(search(*pickup, radius_km=3, limit=1)) == [near_other_cell.pk]


def test_antimeridian_is_handled_by_h3_but_not_bbox():
    d = make_driver(online=True)
    place(d, 0.0, -179.9995)
    assert ids(search(0.0, 179.9995, radius_km=1, strategy="h3")) == [d.pk]
    # Known limitation of the retained bbox fallback.
    assert search(0.0, 179.9995, radius_km=1, strategy="bbox") == []


def test_candidates_near_a_pentagon():
    plat, plng = h3.cell_to_latlng(h3.get_pentagons(8)[0])
    d = make_driver(online=True)
    place(d, plat + 0.01, plng)
    assert ids(search(plat, plng, radius_km=3)) == [d.pk]


# --- filtering ---------------------------------------------------------------------
def test_filters_freshness_availability_category_vehicle_and_trips(settings):
    settings.DRIVER_LOCATION_STALE_SECONDS = 120
    ok = make_driver(online=True)
    stale = make_driver(online=True)
    offline = make_driver(online=True)
    xl = make_driver(online=True, category="XL")
    no_vehicle = make_driver(online=True)
    on_trip = make_driver(online=True)
    for i, d in enumerate([ok, stale, offline, xl, no_vehicle, on_trip]):
        place(d, SF[0] + 0.001 * i, SF[1], seconds_ago=600 if d is stale else 0)
    DriverProfile.objects.filter(pk=offline.pk).update(availability_status="OFFLINE")
    no_vehicle.vehicles.update(is_active=False)
    Trip.objects.create(
        rider=make_rider(), driver=on_trip, status=TripStatus.DRIVER_ARRIVING,
        pickup_latitude=1, pickup_longitude=1, dropoff_latitude=2, dropoff_longitude=2,
        ride_category="ECONOMY", estimated_fare=1, estimated_distance_km=1,
        estimated_duration_minutes=1, currency="USD",
    )  # fmt: skip
    assert ids(search(category="ECONOMY")) == [ok.pk]
    assert ids(search(category="XL")) == [xl.pk]
    assert set(ids(search())) == {ok.pk, xl.pk}


def test_drivers_beyond_radius_excluded_even_inside_scanned_cells():
    near, far = make_driver(online=True), make_driver(online=True)
    place(near, SF[0] + 0.005, SF[1])  # ~0.56 km
    place(far, SF[0] + 0.02, SF[1])  # ~2.2 km
    assert ids(search(radius_km=1)) == [near.pk]


def test_results_sorted_by_straight_line_distance_and_limited():
    drivers = [make_driver(online=True) for _ in range(5)]
    for i, d in enumerate(drivers):
        place(d, SF[0] + 0.003 * (5 - i), SF[1])
    result = search(limit=3)
    assert ids(result) == [drivers[4].pk, drivers[3].pk, drivers[2].pk]
    assert [c.distance_km for c in result] == sorted(c.distance_km for c in result)


# --- bounded expansion ---------------------------------------------------------------
def test_expansion_stops_early_when_enough_close_candidates(recorded_metrics):
    for _ in range(3):
        place(make_driver(online=True), SF[0] + 0.0005, SF[1])
    search(radius_km=20, limit=3)
    rings = [v for (name, _), v in recorded_metrics.gauges.items() if "rings_scanned" in name]
    assert rings and rings[0] <= 3


def test_expansion_is_bounded_by_radius_when_area_is_empty(recorded_metrics):
    geo = get_geo_search_service()
    max_k = geo.max_rings_for_radius(*SF, 2.0)
    assert search(radius_km=2.0) == []
    rings = [v for (name, _), v in recorded_metrics.gauges.items() if "rings_scanned" in name]
    assert 1 <= rings[0] <= max_k + 1


# --- fallback --------------------------------------------------------------------------
def test_large_radius_falls_back_to_bbox(settings, recorded_metrics):
    settings.H3_MAX_GRID_DISTANCE = 3
    d = make_driver(online=True)
    place(d, SF[0] + 0.05, SF[1])
    assert ids(search(radius_km=10)) == [d.pk]
    assert (
        recorded_metrics.count(
            "drivers.candidates.fallback", reason="radius_exceeds_max_grid_distance"
        )
        == 1
    )
    assert recorded_metrics.count("drivers.candidates.search", strategy="bbox") == 1


def test_h3_failure_falls_back_to_bbox(monkeypatch, recorded_metrics):
    d = make_driver(online=True)
    place(d, SF[0] + 0.001, SF[1])

    def boom(*args):
        raise h3.H3FailedError("simulated")

    monkeypatch.setattr(h3, "grid_ring", boom)
    monkeypatch.setattr(h3, "grid_disk", boom)
    assert ids(search()) == [d.pk]
    assert recorded_metrics.count("drivers.candidates.fallback", reason="h3_error") == 1


def test_unknown_strategy_rejected():
    with pytest.raises(ValueError):
        search(strategy="quadtree")


# --- parity with the retained bbox implementation ----------------------------------------
@pytest.mark.parametrize("resolution", [7, 8, 9])
def test_h3_matches_bbox_on_random_fleet(settings, resolution):
    settings.H3_RESOLUTION = resolution
    settings.H3_MAX_GRID_DISTANCE = 100
    rng = random.Random(resolution)
    for _ in range(60):
        d = make_driver(online=True, category=rng.choice(["ECONOMY", "XL"]))
        place(d, SF[0] + rng.uniform(-0.06, 0.06), SF[1] + rng.uniform(-0.07, 0.07))
    for _ in range(6):
        pickup = (SF[0] + rng.uniform(-0.03, 0.03), SF[1] + rng.uniform(-0.03, 0.03))
        for radius, limit, category in [(1.0, 50, None), (3.0, 5, None), (2.5, 50, "XL")]:
            kw = {"radius_km": radius, "limit": limit, "category": category}
            assert ids(search(*pickup, strategy="h3", **kw)) == ids(
                search(*pickup, strategy="bbox", **kw)
            )


# --- resolution changes -----------------------------------------------------------------
def test_resolution_change_needs_reindex_then_works(settings):
    d = make_driver(online=True)
    place(d, SF[0] + 0.001, SF[1])  # indexed at resolution 8
    settings.H3_RESOLUTION = 9
    assert search(strategy="h3", radius_km=1) == []  # old-resolution cells don't match
    call_command("reindex_driver_locations", stdout=StringIO())
    row = latest(d)
    assert row.h3_resolution == 9 and h3.get_resolution(row.h3_cell) == 9
    assert ids(search(strategy="h3", radius_km=1)) == [d.pk]


def test_straight_line_distance_is_reported_not_road_distance():
    d = make_driver(online=True)
    place(d, SF[0] + 0.01, SF[1])
    [c] = search()
    assert c.distance_km == pytest.approx(haversine_km(*SF, SF[0] + 0.01, SF[1]), rel=1e-6)
