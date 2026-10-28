"""Location service: the single entry point for driver location reads and writes.

Update flow (see docs/location-service.md and docs/redis-hot-store.md):

    Driver app --POST /api/v1/locations/update/--> LocationUpdateView
        -> LocationUpdateSerializer   (shape, ranges, future-timestamp guard)
        -> LocationService.record_driver_update
             1. normalise timestamp (clamp to server time)
             2. drop fixes that are too old / too inaccurate     (accepted=false)
             3. H3 cell for the fix (on failure the fix is still stored, without a cell:
                invisible to H3 search until the next fix, visible to the bbox strategy)
             4. LocationStore.upsert_if_newer: atomic "driver ONLINE and fix newer"
                check + write + cell move (Redis Lua / PostgreSQL row lock)
                                                                 (409 driver_offline)
             5. (unknown status in Redis -> seed from PostgreSQL, retry once)
             6. sampled DriverLocationEvent history              (optional)
             7. structured log + metrics
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from apps.common.exceptions import ConflictError
from apps.common.geo import bounding_box, haversine_km
from apps.common.metrics import metrics, timed
from apps.geoindex.h3_index import GeoIndexError
from apps.geoindex.service import GeoSearchService, get_geo_search_service

from .failover import FailoverLocationStore
from .models import DriverLocationEvent
from .stores import (
    BoundingBox,
    HistoryPoint,
    LocationFix,
    LocationStore,
    PostgresLocationStore,
    WriteStatus,
)

logger = logging.getLogger("apps.locations")


class DriverOfflineError(ConflictError):
    default_detail = "Go online before sharing your location."
    default_code = "driver_offline"


class UpdateOutcome(str, enum.Enum):
    APPLIED = "applied"
    OUT_OF_ORDER = "out_of_order"  # older than or equal to the stored fix
    TOO_OLD = "too_old"  # device timestamp older than LOCATION_MAX_AGE_SECONDS
    LOW_ACCURACY = "low_accuracy"  # accuracy worse than LOCATION_MAX_ACCURACY_M


class Freshness(str, enum.Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"


@dataclass(frozen=True)
class UpdateResult:
    outcome: UpdateOutcome
    location: LocationFix | None  # what is stored after the call
    history_recorded: bool = False

    @property
    def accepted(self) -> bool:
        return self.outcome is UpdateOutcome.APPLIED


class LocationService:
    def __init__(
        self, store: LocationStore, geo: GeoSearchService | None = None, clock=timezone.now
    ):
        self.store = store
        self.geo = geo
        self.clock = clock

    # --- writes -----------------------------------------------------------
    def record_driver_update(
        self,
        *,
        driver,
        latitude: Decimal,
        longitude: Decimal,
        recorded_at: datetime | None = None,
        heading: float | None = None,
        speed_kmh: float | None = None,
        accuracy_m: float | None = None,
    ) -> UpdateResult:
        with timed("location.update.duration_ms"):
            now = self.clock()
            fix = LocationFix(
                driver_id=driver.pk,
                latitude=latitude,
                longitude=longitude,
                # Missing device time -> server time. Small forward drift is clamped so a
                # fast device clock can neither extend freshness nor block later fixes.
                recorded_at=min(recorded_at or now, now),
                heading=heading,
                speed_kmh=speed_kmh,
                accuracy_m=accuracy_m,
            )

            if now - fix.recorded_at > timedelta(seconds=settings.LOCATION_MAX_AGE_SECONDS):
                return self._ignored(fix, UpdateOutcome.TOO_OLD)
            if accuracy_m is not None and accuracy_m > settings.LOCATION_MAX_ACCURACY_M:
                return self._ignored(fix, UpdateOutcome.LOW_ACCURACY)

            fix = self._with_cell(fix)
            # The store enforces "driver must be ONLINE" atomically with the write.
            result = self.store.upsert_if_newer(fix)
            if result.status is WriteStatus.DRIVER_UNKNOWN:
                # Live store lost the driver's status (Redis restart / TTL expiry):
                # re-seed from PostgreSQL, the source of truth, and retry once.
                self._seed_availability(driver.pk)
                result = self.store.upsert_if_newer(fix)
            if result.status in (WriteStatus.DRIVER_OFFLINE, WriteStatus.DRIVER_UNKNOWN):
                self._observe(driver.pk, "rejected_offline")
                raise DriverOfflineError()
            if result.status is WriteStatus.NOT_NEWER:
                return self._ignored(fix, UpdateOutcome.OUT_OF_ORDER, current=result.current)

            history = self._maybe_record_history(result.current)

        self._observe(driver.pk, UpdateOutcome.APPLIED.value, history_recorded=history)
        return UpdateResult(UpdateOutcome.APPLIED, result.current, history_recorded=history)

    def set_driver_availability(self, driver_id: int, *, online: bool, category: str | None):
        """Mirror an availability change into the live store. Going offline also
        drops the location and every index entry (atomically in Redis)."""
        self.store.set_availability(driver_id, online=online, category=category)
        if not online:
            logger.info("location.cleared", extra={"driver_id": driver_id})

    def _seed_availability(self, driver_id: int) -> None:
        from apps.drivers.services import live_availability_snapshot

        online, category = live_availability_snapshot(driver_id)
        self.store.seed_availability(driver_id, online=online, category=category)
        metrics().increment("location.store.availability_seeded")

    def clear(self, driver_id: int) -> None:
        """Forget a driver's latest location (e.g. when they go offline)."""
        self.store.remove(driver_id)
        logger.info("location.cleared", extra={"driver_id": driver_id})

    # --- reads ------------------------------------------------------------
    def get_latest(self, driver_id: int) -> LocationFix | None:
        return self.store.get(driver_id)

    def stale_cutoff(self) -> datetime:
        return self.clock() - timedelta(seconds=settings.DRIVER_LOCATION_STALE_SECONDS)

    def freshness(self, fix: LocationFix | None) -> Freshness:
        if fix is None:
            return Freshness.MISSING
        return Freshness.FRESH if fix.recorded_at >= self.stale_cutoff() else Freshness.STALE

    def find_nearby(
        self, *, lat: float, lng: float, radius_km: float
    ) -> list[tuple[LocationFix, float]]:
        """Fresh fixes within `radius_km`, nearest first, as (fix, distance_km)."""
        bbox = BoundingBox(*bounding_box(lat, lng, radius_km))
        results = []
        for fix in self.store.find_in_bbox(bbox, fresh_after=self.stale_cutoff()):
            distance = haversine_km(lat, lng, float(fix.latitude), float(fix.longitude))
            if distance <= radius_km:
                results.append((fix, distance))
        results.sort(key=lambda item: item[1])
        return results

    def find_in_cells(self, cells: list[str]) -> list[LocationFix]:
        """Fresh fixes located in any of `cells` (unordered, unfiltered by distance)."""
        return self.store.find_in_cells(cells, fresh_after=self.stale_cutoff())

    # --- internals --------------------------------------------------------
    def _with_cell(self, fix: LocationFix) -> LocationFix:
        if self.geo is None:
            return fix
        try:
            cell = self.geo.cell_for(float(fix.latitude), float(fix.longitude))
        except GeoIndexError:
            # Never lose a valid GPS fix because indexing failed.
            metrics().increment("location.h3.index_error")
            logger.warning("location.h3.index_error", extra={"driver_id": fix.driver_id})
            return fix
        return replace(fix, h3_cell=cell, h3_resolution=self.geo.resolution)

    def _ignored(self, fix, outcome, current=None) -> UpdateResult:
        self._observe(fix.driver_id, outcome.value)
        if current is None:
            current = self.store.get(fix.driver_id)
        return UpdateResult(outcome, current)

    def _maybe_record_history(self, fix: LocationFix) -> bool:
        if not settings.LOCATION_HISTORY_ENABLED:
            return False
        # The marker lives in the hot store, so no PostgreSQL read per GPS ping. If it
        # is missing (e.g. after a Redis restart) we record one extra sample.
        last = self.store.last_history_point(fix.driver_id)
        if last is not None:
            elapsed = (fix.recorded_at - last.recorded_at).total_seconds()
            moved_m = 1000 * haversine_km(
                float(last.latitude),
                float(last.longitude),
                float(fix.latitude),
                float(fix.longitude),
            )
            moved_enough = (
                elapsed >= settings.LOCATION_HISTORY_MIN_INTERVAL_SECONDS
                and moved_m >= settings.LOCATION_HISTORY_MIN_DISTANCE_M
            )
            heartbeat_due = elapsed >= settings.LOCATION_HISTORY_MAX_INTERVAL_SECONDS
            if not (moved_enough or heartbeat_due):
                return False

        DriverLocationEvent.objects.create(
            driver_id=fix.driver_id,
            latitude=fix.latitude,
            longitude=fix.longitude,
            heading=fix.heading,
            speed_kmh=fix.speed_kmh,
            accuracy_m=fix.accuracy_m,
            recorded_at=fix.recorded_at,
        )
        self.store.mark_history_point(
            fix.driver_id, HistoryPoint(fix.latitude, fix.longitude, fix.recorded_at)
        )
        metrics().increment("location.history.recorded")
        return True

    @staticmethod
    def _observe(driver_id: int, outcome: str, **extra) -> None:
        metrics().increment("location.update", tags={"outcome": outcome})
        # Coordinates are personal data: they are deliberately not logged.
        level = logging.DEBUG if outcome == UpdateOutcome.APPLIED.value else logging.INFO
        logger.log(
            level,
            "location.update",
            extra={"driver_id": driver_id, "outcome": outcome, **extra},
        )


def build_store() -> LocationStore:
    """Store selected by settings (read on each call so tests can override)."""
    return _build_store(
        settings.LOCATION_STORE,
        settings.LOCATION_STORE_FALLBACK,
        settings.REDIS_URL,
        settings.REDIS_KEY_PREFIX,
        settings.LOCATION_STORE_CIRCUIT_COOLDOWN_SECONDS,
    )


@lru_cache(maxsize=8)
def _build_store(kind, fallback, redis_url, prefix, cooldown) -> LocationStore:
    # One instance per configuration and process: the circuit-breaker state and the
    # Redis connection pool must be shared by all requests.
    if kind == "postgres":
        return PostgresLocationStore()
    if kind != "redis":
        raise ImproperlyConfigured(f"LOCATION_STORE must be 'redis' or 'postgres', not {kind!r}")
    from .redis_store import RedisLocationStore

    primary = RedisLocationStore(prefix=prefix)
    if fallback == "none":
        return primary
    if fallback != "postgres":
        raise ImproperlyConfigured("LOCATION_STORE_FALLBACK must be 'postgres' or 'none'")

    def on_recovery(since):
        from apps.drivers.services import resync_live_availability

        resync_live_availability(changed_since=since)

    return FailoverLocationStore(
        primary, PostgresLocationStore(), cooldown_seconds=cooldown, on_recovery=on_recovery
    )


def get_location_service() -> LocationService:
    return LocationService(build_store(), geo=get_geo_search_service())
