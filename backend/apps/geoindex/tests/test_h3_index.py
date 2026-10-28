import math
import random

import h3
import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.common.geo import haversine_km
from apps.geoindex.h3_index import GeoIndexError, H3GeoIndex
from apps.geoindex.service import GeoSearchService, get_geo_search_service

SF = (37.7749, -122.4194)
idx = H3GeoIndex(8)


# --- conversion ------------------------------------------------------------------
def test_cell_for_returns_valid_cell_at_resolution():
    cell = idx.cell_for(*SF)
    assert h3.is_valid_cell(cell) and h3.get_resolution(cell) == 8
    assert idx.cell_for(*SF) == cell  # deterministic


def test_resolution_is_configurable():
    fine = H3GeoIndex(9).cell_for(*SF)
    assert h3.get_resolution(fine) == 9
    assert h3.cell_to_parent(fine, 8) == idx.cell_for(*SF)


def test_accepts_decimal_and_string_numbers():
    from decimal import Decimal

    assert idx.cell_for(Decimal("37.7749"), Decimal("-122.4194")) == idx.cell_for(*SF)
    assert idx.cell_for("37.7749", "-122.4194") == idx.cell_for(*SF)


@pytest.mark.parametrize(
    "lat, lng",
    [
        (95, 0),
        (-90.0001, 0),
        (0, 180.5),
        (0, -181),
        (math.nan, 0),
        (0, math.inf),
        ("x", 0),
        (None, 0),
    ],
)
def test_invalid_coordinates_raise_geoindex_error(lat, lng):
    # h3 itself would silently wrap lat=95 into a valid cell.
    with pytest.raises(GeoIndexError):
        idx.cell_for(lat, lng)


@pytest.mark.parametrize("lat, lng", [(90, 0), (-90, 0), (0, 180), (0, -180), (0, 0)])
def test_extreme_but_valid_coordinates(lat, lng):
    assert h3.is_valid_cell(idx.cell_for(lat, lng))


@pytest.mark.parametrize("resolution", [-1, 16, "8", 8.0, None])
def test_invalid_resolution_rejected(resolution):
    with pytest.raises(GeoIndexError):
        H3GeoIndex(resolution)


def test_bad_resolution_setting_is_improperly_configured(settings):
    settings.H3_RESOLUTION = 99
    with pytest.raises(ImproperlyConfigured):
        get_geo_search_service()


@pytest.mark.parametrize("cell", ["zzz", "", None, 12345, "8828308281fffff0"])
def test_invalid_cells_rejected(cell):
    with pytest.raises(GeoIndexError):
        idx.disk(cell, 1)


def test_cell_of_other_resolution_rejected():
    with pytest.raises(GeoIndexError):
        idx.ring(H3GeoIndex(9).cell_for(*SF), 1)


# --- neighbours ------------------------------------------------------------------
def test_disk_and_ring_sizes():
    c = idx.cell_for(*SF)
    assert [len(idx.disk(c, k)) for k in range(4)] == [1, 7, 19, 37]
    assert [len(idx.ring(c, k)) for k in range(4)] == [1, 6, 12, 18]


def test_ring_is_disk_difference_and_neighbours_are_adjacent():
    c = idx.cell_for(*SF)
    for k in (1, 2, 5):
        assert set(idx.ring(c, k)) == set(idx.disk(c, k)) - set(idx.disk(c, k - 1))
    assert all(idx.grid_distance(c, n) == 1 for n in idx.ring(c, 1))


def test_negative_or_non_int_k_rejected():
    c = idx.cell_for(*SF)
    for k in (-1, 1.5, "1"):
        with pytest.raises(GeoIndexError):
            idx.disk(c, k)


def test_pentagon_neighbourhood():
    pentagon = h3.get_pentagons(8)[0]
    assert len(idx.disk(pentagon, 1)) == 6  # 5 neighbours, not 6
    assert len(idx.ring(pentagon, 1)) == 5
    for k in (1, 2, 3):
        assert set(idx.ring(pentagon, k)) == set(idx.disk(pentagon, k)) - set(
            idx.disk(pentagon, k - 1)
        )


def test_ring_falls_back_to_disk_difference_on_h3_failure(monkeypatch):
    c = idx.cell_for(*SF)
    expected = set(idx.ring(c, 2))

    def boom(*args):
        raise h3.H3FailedError("simulated pentagon distortion")

    monkeypatch.setattr(h3, "grid_ring", boom)
    assert set(idx.ring(c, 2)) == expected


def test_h3_errors_are_wrapped(monkeypatch):
    c = idx.cell_for(*SF)

    def boom(*args):
        raise h3.H3FailedError("simulated")

    monkeypatch.setattr(h3, "grid_disk", boom)
    with pytest.raises(GeoIndexError):
        idx.disk(c, 1)
    monkeypatch.setattr(h3, "latlng_to_cell", boom)
    with pytest.raises(GeoIndexError):
        idx.cell_for(*SF)


def test_grid_distance_returns_none_when_h3_cannot_compute():
    a = idx.cell_for(*SF)
    b = idx.cell_for(-33.86, 151.2)  # Sydney: other side of the planet
    assert idx.grid_distance(a, b) is None or idx.grid_distance(a, b) > 1000


def test_antimeridian_cells():
    # A cell may straddle 180 degrees: points either side share it.
    assert idx.cell_for(0, 179.9999) == idx.cell_for(0, -179.9999)
    # Points ~1 km apart across the antimeridian are a few hops apart, not ~360 deg.
    east, west = idx.cell_for(0, 179.996), idx.cell_for(0, -179.996)
    assert idx.grid_distance(east, west) <= 2
    assert west in idx.disk(east, 2)


# --- geographic service ------------------------------------------------------------
def test_search_area_returns_center_and_neighbours():
    area = GeoSearchService(idx).search_area(*SF, k=1)
    assert area.center_cell == idx.cell_for(*SF) and area.resolution == 8
    assert len(area.neighbors) == 6 and area.center_cell not in area.neighbors
    assert set(area.cells) == set(idx.disk(area.center_cell, 1))
    assert area.rings[0] == [area.center_cell]


def test_expanding_rings_are_bounded_and_ordered():
    steps = list(GeoSearchService(idx).expanding_rings(*SF, max_k=3))
    assert [s.k for s in steps] == [0, 1, 2, 3]
    assert [s.min_distance_km for s in steps] == sorted(s.min_distance_km for s in steps)
    assert steps[0].min_distance_km == steps[1].min_distance_km == 0


# --- geometric bounds (what makes bounded expansion exact) ------------------------
def _random_point_near(rng, lat, lng, max_km):
    bearing = rng.uniform(0, 2 * math.pi)
    d = max_km * math.sqrt(rng.random())
    dlat = (d / 111.32) * math.cos(bearing)
    dlng = (d / (111.32 * math.cos(math.radians(lat)))) * math.sin(bearing)
    return lat + dlat, lng + dlng


@pytest.mark.parametrize("resolution", [7, 8, 9])
@pytest.mark.parametrize("origin", [(0.0, 0.0), SF, (59.33, 18.07), (-54.8, -68.3), (71.0, 25.0)])
def test_rings_to_cover_and_min_distance_bounds_hold(resolution, origin):
    index = H3GeoIndex(resolution)
    rng = random.Random(f"{resolution}{origin}")
    radius = 3 * index.edge_length_km(index.cell_for(*origin)) * 4
    for _ in range(300):
        pickup = _random_point_near(rng, *origin, 0.5)
        center = index.cell_for(*pickup)
        k_max = index.rings_to_cover(center, radius)
        point = _random_point_near(rng, *pickup, radius)
        d = haversine_km(*pickup, *point)
        k = index.grid_distance(center, index.cell_for(*point))
        assert k is not None and k <= k_max, (pickup, point)
        assert d >= index.min_distance_km_at(center, k) - 1e-9, (pickup, point, k)
