"""Driver candidate retrieval: "who could serve a pickup here?"

Separation of concerns:
  * apps.geoindex   - H3 cells, rings, geometric bounds (no business rules)
  * apps.locations  - latest fixes per cell, freshness (no business rules)
  * this module     - availability rules (ONLINE, active vehicle, category, not on a
                      trip) and the bounded expansion policy.
The matching engine (later step) consumes `Candidate`s and never sees H3, so the
resolution can change via settings without code changes here or there.

Distances: `distance_km` is straight-line (great-circle) distance and
`grid_distance` is H3 hops. NEITHER is road distance or travel time; an ETA from a
routing engine must be used before making dispatch decisions on them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Exists, OuterRef

from apps.common.metrics import metrics
from apps.geoindex.h3_index import GeoIndexError
from apps.geoindex.service import get_geo_search_service
from apps.locations.service import get_location_service
from apps.locations.stores import LocationFix

from .models import AvailabilityStatus, Vehicle

logger = logging.getLogger("apps.drivers")

STRATEGY_H3 = "h3"
STRATEGY_BBOX = "bbox"


@dataclass(frozen=True)
class Candidate:
    driver_id: int
    public_id: str
    category: str
    latitude: float
    longitude: float
    heading: float | None
    recorded_at: object  # datetime of the fix used
    distance_km: float  # straight-line, NOT road distance
    grid_distance: int | None  # H3 hops from the pickup cell; None for bbox search


class _FallbackToBBox(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _eligible(driver_ids: set[int], category: str | None) -> dict[int, tuple[str, str]]:
    """driver_id -> (public_id, vehicle category) for drivers who may take a ride now."""
    if not driver_ids:
        return {}
    from apps.trips.models import DRIVER_ACTIVE_STATUSES, Trip

    on_trip = Trip.objects.filter(
        driver_id=OuterRef("driver_id"), status__in=DRIVER_ACTIVE_STATUSES
    )
    filters = {
        "driver_id__in": driver_ids,
        "is_active": True,
        "driver__availability_status": AvailabilityStatus.ONLINE,
    }
    if category:
        filters["category"] = category
    rows = (
        Vehicle.objects.filter(**filters)
        .exclude(Exists(on_trip))
        .values_list("driver_id", "driver__public_id", "category")
    )
    return {driver_id: (str(public_id), cat) for driver_id, public_id, cat in rows}


def _to_candidates(
    fixes_with_distance: list[tuple[LocationFix, float, int | None]],
    category: str | None,
) -> list[Candidate]:
    eligible = _eligible({fix.driver_id for fix, _, _ in fixes_with_distance}, category)
    out = []
    for fix, distance, grid_distance in fixes_with_distance:
        if fix.driver_id not in eligible:
            continue
        public_id, vehicle_category = eligible[fix.driver_id]
        out.append(
            Candidate(
                driver_id=fix.driver_id,
                public_id=public_id,
                category=vehicle_category,
                latitude=float(fix.latitude),
                longitude=float(fix.longitude),
                heading=fix.heading,
                recorded_at=fix.recorded_at,
                distance_km=distance,
                grid_distance=grid_distance,
            )
        )
    return out


def _bbox_candidates(*, lat, lng, radius_km, category, limit) -> list[Candidate]:
    """Step 1/2 implementation, retained as the fallback."""
    nearby = get_location_service().find_nearby(lat=lat, lng=lng, radius_km=radius_km)
    candidates = _to_candidates([(fix, d, None) for fix, d in nearby], category)
    return candidates[:limit]


def _h3_candidates(*, lat, lng, radius_km, category, limit) -> list[Candidate]:
    """Bounded ring-by-ring expansion around the pickup cell.

    Stops when either
      * `limit` candidates are found AND none of the remaining rings can contain a
        point closer than the current `limit`-th candidate (exact nearest-N), or
      * every ring that can intersect `radius_km` has been scanned, or
      * the hard cap H3_MAX_GRID_DISTANCE would be exceeded (-> bbox fallback).
    Ring 1 is always scanned: a driver 20 m away may sit across a cell boundary.
    """
    geo = get_geo_search_service()
    locations = get_location_service()
    try:
        max_k = geo.max_rings_for_radius(lat, lng, radius_km)
        if max_k > settings.H3_MAX_GRID_DISTANCE:
            raise _FallbackToBBox("radius_exceeds_max_grid_distance")
        found: list[Candidate] = []
        rings_scanned = 0
        for step in geo.expanding_rings(lat, lng, max_k=max_k):
            if step.k >= 2 and len(found) >= limit:
                if found[limit - 1].distance_km <= step.min_distance_km:
                    break  # nothing in this or further rings can be closer
            if step.min_distance_km > radius_km:
                break
            rings_scanned += 1
            batch = []
            for fix in locations.find_in_cells(step.cells):
                distance = _straight_line_km(lat, lng, fix)
                if distance <= radius_km:
                    batch.append((fix, distance, step.k))
            found.extend(_to_candidates(batch, category))
            found.sort(key=lambda c: c.distance_km)
    except GeoIndexError as exc:
        raise _FallbackToBBox("h3_error") from exc
    metrics().gauge("drivers.candidates.rings_scanned", rings_scanned)
    return found[:limit]


def _straight_line_km(lat: float, lng: float, fix: LocationFix) -> float:
    from apps.common.geo import haversine_km

    return haversine_km(lat, lng, float(fix.latitude), float(fix.longitude))


def find_candidate_drivers(
    *,
    lat: float,
    lng: float,
    radius_km: float,
    category: str | None = None,
    limit: int,
    strategy: str | None = None,
) -> list[Candidate]:
    """Available drivers with a fresh location within `radius_km`, nearest first."""
    strategy = strategy or settings.NEARBY_SEARCH_STRATEGY
    args = {"lat": lat, "lng": lng, "radius_km": radius_km, "category": category, "limit": limit}
    if strategy == STRATEGY_H3:
        try:
            result = _h3_candidates(**args)
        except _FallbackToBBox as fallback:
            metrics().increment("drivers.candidates.fallback", tags={"reason": fallback.reason})
            logger.warning("drivers.candidates.fallback", extra={"reason": fallback.reason})
            strategy = STRATEGY_BBOX
            result = _bbox_candidates(**args)
    elif strategy == STRATEGY_BBOX:
        result = _bbox_candidates(**args)
    else:
        raise ValueError(f"Unknown NEARBY_SEARCH_STRATEGY {strategy!r}")
    metrics().increment("drivers.candidates.search", tags={"strategy": strategy})
    metrics().gauge("drivers.candidates.results", len(result))
    return result
