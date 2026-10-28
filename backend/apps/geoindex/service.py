"""Geographic search service: pickup point -> cells to scan, ring by ring.

Business code (driver candidate search, later matching/surge) talks to this
service, never to h3 directly.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .h3_index import GeoIndexError, H3GeoIndex


@dataclass(frozen=True)
class SearchArea:
    resolution: int
    center_cell: str
    k: int
    neighbors: list[str]  # every cell in disk(center, k) except the center
    rings: dict[int, list[str]] = field(default_factory=dict)  # distance -> cells

    @property
    def cells(self) -> list[str]:
        return [self.center_cell, *self.neighbors]


@dataclass(frozen=True)
class RingStep:
    k: int
    cells: list[str]
    # No point in this ring (or any further ring) is closer than this, straight-line.
    min_distance_km: float


class GeoSearchService:
    def __init__(self, index: H3GeoIndex):
        self.index = index

    @property
    def resolution(self) -> int:
        return self.index.resolution

    def cell_for(self, lat: float, lng: float) -> str:
        return self.index.cell_for(lat, lng)

    def search_area(self, lat: float, lng: float, k: int = 1) -> SearchArea:
        """Center cell for the pickup plus all neighbouring cells up to k rings."""
        center = self.index.cell_for(lat, lng)
        rings = {d: self.index.ring(center, d) for d in range(k + 1)}
        neighbors = [c for d in range(1, k + 1) for c in rings[d]]
        return SearchArea(self.resolution, center, k, neighbors, rings)

    def max_rings_for_radius(self, lat: float, lng: float, radius_km: float) -> int:
        return self.index.rings_to_cover(self.index.cell_for(lat, lng), radius_km)

    def expanding_rings(self, lat: float, lng: float, *, max_k: int) -> Iterator[RingStep]:
        """Yield ring 0 (the pickup cell), then ring 1, 2 ... up to `max_k` inclusive.

        The caller decides when to stop (bounded expansion); `max_k` is a hard cap.
        """
        center = self.index.cell_for(lat, lng)
        for k in range(max_k + 1):
            yield RingStep(k, self.index.ring(center, k), self.index.min_distance_km_at(center, k))


@lru_cache(maxsize=4)
def _service_for(resolution: int) -> GeoSearchService:
    try:
        return GeoSearchService(H3GeoIndex(resolution))
    except GeoIndexError as exc:
        raise ImproperlyConfigured(f"H3_RESOLUTION: {exc}") from exc


def get_geo_search_service() -> GeoSearchService:
    """Service at the configured resolution (settings read per call: tests can override)."""
    return _service_for(settings.H3_RESOLUTION)
