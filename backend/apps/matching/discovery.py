"""Candidate discovery: Batch Search + K-Ring Search over the H3 live index.

Pure discovery: returns positions near the pickup. It does NOT decide who is
eligible (state.py), how long pickup takes (eta.py) or who wins (ranking.py).
The live index is Redis in production, reached only through LocationService /
LocationStore, so this module has no Redis (or PostgreSQL) code.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from apps.common.geo import haversine_km
from apps.geoindex.service import GeoSearchService, get_geo_search_service
from apps.locations.service import LocationService, get_location_service
from apps.locations.stores import LocationFix


@dataclass(frozen=True)
class RawCandidate:
    fix: LocationFix
    grid_distance: int  # H3 hops from the pickup cell (NOT road distance)
    straight_line_km: float  # great-circle (NOT road distance)

    @property
    def driver_id(self) -> int:
        return self.fix.driver_id


@dataclass(frozen=True)
class RingBatch:
    k: int
    cells: list[str]
    candidates: list[RawCandidate]
    # No point in this or further rings is closer than this (straight line).
    min_distance_km: float


class BatchSearch:
    """One round trip to the live index for a set of cells (fresh fixes only)."""

    def __init__(self, locations: LocationService | None = None):
        self.locations = locations or get_location_service()

    def search(self, cells: list[str]) -> list[LocationFix]:
        return self.locations.find_in_cells(cells)


class KRingSearch:
    """Expands from the pickup cell one hollow ring at a time.

    Bounds: never beyond `max_k` rings and never beyond `max_radius_km` (rings whose
    nearest possible point is further than that are not read). Ring 1 is always
    produced when ring 0 is, because a driver metres away can sit across an edge.
    """

    def __init__(
        self,
        batch: BatchSearch | None = None,
        geo: GeoSearchService | None = None,
    ):
        self.batch = batch or BatchSearch()
        self.geo = geo or get_geo_search_service()

    def rings(
        self, lat: float, lng: float, *, max_k: int, max_radius_km: float
    ) -> Iterator[RingBatch]:
        cap = min(max_k, self.geo.max_rings_for_radius(lat, lng, max_radius_km))
        for step in self.geo.expanding_rings(lat, lng, max_k=cap):
            if step.min_distance_km > max_radius_km:
                return
            found = []
            for fix in self.batch.search(step.cells):
                d = haversine_km(lat, lng, float(fix.latitude), float(fix.longitude))
                if d <= max_radius_km:
                    found.append(RawCandidate(fix, step.k, d))
            yield RingBatch(step.k, step.cells, found, step.min_distance_km)
