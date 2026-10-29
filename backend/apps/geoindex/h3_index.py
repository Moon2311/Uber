"""Thin, defensive wrapper around Uber's H3 library.

This module is the *only* place that imports `h3`. Everything above it works in
terms of opaque cell ids and integer grid distances, so the resolution (or even
the indexing scheme) can change without touching trip matching or pricing code.

IMPORTANT: H3 grid distance counts hexagon hops. It is NOT road distance and NOT
travel time: a driver 2 cells away may be across a river, on a one-way street or
behind a motorway with no exit. Grid distance is only used to *bound and order the
search*; ranking uses great-circle distance today and a routing ETA later.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import h3

MIN_RESOLUTION = 0
MAX_RESOLUTION = 15
# Hexagon sizes vary by up to ~2x across the globe (icosahedron distortion) and
# pentagons are smaller still. Bounds below are padded by this factor.
_SIZE_SAFETY = 1.25


class GeoIndexError(ValueError):
    """Any failure of the geo index (bad input, H3 error). Callers may fall back."""


def _validate_latlng(lat: float, lng: float) -> tuple[float, float]:
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError) as exc:
        raise GeoIndexError(f"Coordinates must be numbers: {lat!r}, {lng!r}") from exc
    # h3 silently wraps out-of-range latitudes, so check explicitly.
    if not (math.isfinite(lat) and math.isfinite(lng)):
        raise GeoIndexError("Coordinates must be finite.")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise GeoIndexError(f"Coordinates out of range: {lat}, {lng}")
    return lat, lng


@dataclass(frozen=True)
class H3GeoIndex:
    resolution: int

    def __post_init__(self):
        if not isinstance(self.resolution, int) or not (
            MIN_RESOLUTION <= self.resolution <= MAX_RESOLUTION
        ):
            raise GeoIndexError(
                f"H3 resolution must be an int in [{MIN_RESOLUTION}, {MAX_RESOLUTION}]"
            )

    # --- conversion ---------------------------------------------------------
    def cell_for(self, lat: float, lng: float) -> str:
        lat, lng = _validate_latlng(lat, lng)
        try:
            return h3.latlng_to_cell(lat, lng, self.resolution)
        except (h3.H3BaseException, ValueError, TypeError) as exc:
            raise GeoIndexError(f"Cannot index {lat}, {lng}: {exc}") from exc

    def validate_cell(self, cell: str) -> str:
        try:
            valid = isinstance(cell, str) and h3.is_valid_cell(cell)
        except (h3.H3BaseException, ValueError, TypeError):
            valid = False
        if not valid:
            raise GeoIndexError(f"Invalid H3 cell: {cell!r}")
        if h3.get_resolution(cell) != self.resolution:
            raise GeoIndexError(
                f"Cell {cell} has resolution {h3.get_resolution(cell)}, "
                f"index uses {self.resolution}"
            )
        return cell

    def cell_center(self, cell: str) -> tuple[float, float]:
        return h3.cell_to_latlng(self.validate_cell(cell))

    def children(self, cell: str, child_resolution: int) -> list[str]:
        """Cells at a finer resolution that make up `cell` (e.g. a surge zone's
        index cells)."""
        cell = self.validate_cell(cell)
        if not (self.resolution <= child_resolution <= MAX_RESOLUTION):
            raise GeoIndexError(f"Child resolution must be in [{self.resolution}, 15]")
        return list(h3.cell_to_children(cell, child_resolution))

    # --- neighbourhoods -----------------------------------------------------
    def disk(self, cell: str, k: int) -> list[str]:
        """All cells within grid distance k (inclusive), center included."""
        self._check_k(k)
        try:
            return list(h3.grid_disk(self.validate_cell(cell), k))
        except h3.H3BaseException as exc:
            raise GeoIndexError(f"grid_disk({cell}, {k}) failed: {exc}") from exc

    def ring(self, cell: str, k: int) -> list[str]:
        """Cells at exactly grid distance k (hollow ring).

        Near pentagons a ring has fewer than 6k cells. If H3 cannot build the ring
        directly we fall back to disk(k) minus disk(k-1), which is always correct.
        """
        self._check_k(k)
        cell = self.validate_cell(cell)
        if k == 0:
            return [cell]
        try:
            return list(h3.grid_ring(cell, k))
        except h3.H3BaseException:
            try:
                return list(set(h3.grid_disk(cell, k)) - set(h3.grid_disk(cell, k - 1)))
            except h3.H3BaseException as exc:
                raise GeoIndexError(f"grid ring {k} around {cell} failed: {exc}") from exc

    def grid_distance(self, a: str, b: str) -> int | None:
        """Hops between two cells, or None when H3 cannot compute it (far apart /
        across pentagon distortion). Never a road distance."""
        try:
            return h3.grid_distance(self.validate_cell(a), self.validate_cell(b))
        except h3.H3BaseException:
            return None

    # --- geometry bounds (used to keep expansion bounded and exact) ----------
    def edge_length_km(self, cell: str) -> float:
        """Local hexagon edge length derived from the actual cell's area."""
        area = h3.cell_area(self.validate_cell(cell), unit="km^2")
        return math.sqrt(2 * area / (3 * math.sqrt(3)))

    def rings_to_cover(self, cell: str, radius_km: float) -> int:
        """Smallest k such that disk(cell, k) contains every point within
        `radius_km` of *any* point inside `cell`.

        A point at straight-line distance d from the pickup lies in a cell whose
        center is at most d + 2·edge from the pickup cell's center; in a hex grid
        cells k hops apart have centers at least 1.5·edge·k apart.
        """
        if radius_km < 0:
            raise GeoIndexError("radius_km must be >= 0")
        edge = self.edge_length_km(cell)
        e_lo, e_hi = edge / _SIZE_SAFETY, edge * _SIZE_SAFETY
        return max(1, math.ceil((radius_km + 2 * e_hi) / (1.5 * e_lo)))

    def min_distance_km_at(self, cell: str, k: int) -> float:
        """Lower bound on the straight-line distance from any point in `cell` to
        any point in a cell exactly k hops away. 0 for k <= 1."""
        self._check_k(k)
        edge = self.edge_length_km(cell)
        e_lo, e_hi = edge / _SIZE_SAFETY, edge * _SIZE_SAFETY
        return max(0.0, 1.5 * e_lo * k - 2 * e_hi)

    @staticmethod
    def _check_k(k: int) -> None:
        if not isinstance(k, int) or k < 0:
            raise GeoIndexError(f"k must be a non-negative int, got {k!r}")
