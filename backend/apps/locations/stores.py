"""Storage abstraction for *live* driver state (latest fix, availability mirror, H3
cell membership).

`LocationService` and everything above it (candidate search, the future matching
engine) depend only on `LocationStore`. Implementations:

* PostgresLocationStore  - durable table, used alone or as the Redis fallback
* RedisLocationStore     - hot store (apps/locations/redis_store.py)
* FailoverLocationStore  - Redis primary + PostgreSQL fallback with a circuit breaker
                           (apps/locations/failover.py)

Contract every implementation must honour (tests/test_store.py runs against each):

* `upsert_if_newer` is atomic per driver: the fix is applied only when the driver
  is ONLINE *in the store's view* and the incoming `recorded_at` is strictly newer
  than the stored one; equal timestamps are no-ops (idempotent retries). The H3 cell
  membership moves together with the fix.
* `set_availability(offline)` removes the location and cell membership atomically
  with the status change, so an offline driver can never be a candidate.
* `find_in_cells` / `find_in_bbox` return only ONLINE drivers whose fix satisfies
  `recorded_at >= fresh_after`. Expired / stale entries are never returned even if
  index cleanup has not run yet.
* The store knows nothing about vehicles, trips or business rules.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from django.db import connection, transaction
from django.utils import timezone

from .models import DriverLocation, DriverLocationEvent


class StoreUnavailable(Exception):
    """The store cannot serve the request right now (e.g. Redis down)."""


class WriteStatus(str, enum.Enum):
    APPLIED = "applied"
    NOT_NEWER = "not_newer"  # equal or older recorded_at: ignored (idempotent)
    DRIVER_OFFLINE = "driver_offline"
    DRIVER_UNKNOWN = "driver_unknown"  # store has no availability for the driver


@dataclass(frozen=True)
class LocationFix:
    driver_id: int
    latitude: Decimal
    longitude: Decimal
    recorded_at: datetime
    heading: float | None = None
    speed_kmh: float | None = None
    accuracy_m: float | None = None
    updated_at: datetime | None = None  # server receipt time of the stored fix
    h3_cell: str | None = None
    h3_resolution: int | None = None


@dataclass(frozen=True)
class WriteResult:
    status: WriteStatus
    current: LocationFix | None  # what is stored after the call

    @property
    def applied(self) -> bool:
        return self.status is WriteStatus.APPLIED


@dataclass(frozen=True)
class BoundingBox:
    min_lat: float
    max_lat: float
    min_lng: float
    max_lng: float


@dataclass(frozen=True)
class HistoryPoint:
    latitude: Decimal
    longitude: Decimal
    recorded_at: datetime


# (lat, lng) -> (cell, resolution); raises on failure. Supplied by the service so
# stores never import the geo index.
CellFunction = Callable[[float, float], tuple[str, int]]


class LocationStore(ABC):
    name = "abstract"

    # --- writes ---------------------------------------------------------------
    @abstractmethod
    def upsert_if_newer(self, fix: LocationFix) -> WriteResult: ...

    @abstractmethod
    def set_availability(self, driver_id: int, *, online: bool, category: str | None) -> None:
        """Authoritative mirror write (caller holds the driver row lock in PostgreSQL)."""

    @abstractmethod
    def seed_availability(self, driver_id: int, *, online: bool, category: str | None) -> None:
        """Like set_availability but only if the store has no status for the driver
        (used to self-heal after a Redis restart without racing a real toggle)."""

    @abstractmethod
    def remove(self, driver_id: int) -> None:
        """Drop the location and cell membership; keep availability. Idempotent."""

    # --- reads ----------------------------------------------------------------
    @abstractmethod
    def get(self, driver_id: int) -> LocationFix | None: ...

    @abstractmethod
    def find_in_bbox(self, bbox: BoundingBox, *, fresh_after: datetime) -> list[LocationFix]: ...

    @abstractmethod
    def find_in_cells(self, cells: list[str], *, fresh_after: datetime) -> list[LocationFix]:
        """ONLINE drivers whose stored cell is in `cells` and fix is fresh."""

    # --- history sampling marker --------------------------------------------------
    @abstractmethod
    def last_history_point(self, driver_id: int) -> HistoryPoint | None: ...

    @abstractmethod
    def mark_history_point(self, driver_id: int, point: HistoryPoint) -> None: ...

    # --- maintenance ---------------------------------------------------------------
    @abstractmethod
    def reindex_cells(self, cell_fn: CellFunction) -> tuple[int, int]:
        """Recompute every stored cell (e.g. after an H3 resolution change).
        Returns (updated, failed)."""

    @abstractmethod
    def prune(self, *, older_than: datetime) -> int:
        """Remove fixes (and index entries) recorded before `older_than`."""

    @abstractmethod
    def health(self) -> dict: ...


# --------------------------------------------------------------------------------------
# PostgreSQL implementation
# --------------------------------------------------------------------------------------
def _from_model(row: DriverLocation) -> LocationFix:
    return LocationFix(
        driver_id=row.driver_id,
        latitude=row.latitude,
        longitude=row.longitude,
        recorded_at=row.recorded_at,
        heading=row.heading,
        speed_kmh=row.speed_kmh,
        accuracy_m=row.accuracy_m,
        updated_at=row.updated_at,
        h3_cell=row.h3_cell,
        h3_resolution=row.h3_resolution,
    )


class PostgresLocationStore(LocationStore):
    """One row per driver in `locations_driverlocation`, written with a guarded UPSERT.

    Availability is read straight from `drivers_driverprofile` (the source of truth),
    so set/seed_availability only need to clear the location when going offline.
    """

    name = "postgres"
    _table = DriverLocation._meta.db_table

    _UPSERT_SQL = f"""
        INSERT INTO {_table}
            (driver_id, latitude, longitude, heading, speed_kmh, accuracy_m,
             recorded_at, updated_at, h3_cell, h3_resolution)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (driver_id) DO UPDATE SET
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            heading = EXCLUDED.heading,
            speed_kmh = EXCLUDED.speed_kmh,
            accuracy_m = EXCLUDED.accuracy_m,
            recorded_at = EXCLUDED.recorded_at,
            updated_at = EXCLUDED.updated_at,
            h3_cell = EXCLUDED.h3_cell,
            h3_resolution = EXCLUDED.h3_resolution
        WHERE {_table}.recorded_at < EXCLUDED.recorded_at
        RETURNING driver_id
    """  # noqa: S608 - table name is a constant from model metadata, values are bound

    def upsert_if_newer(self, fix: LocationFix) -> WriteResult:
        from apps.drivers.models import AvailabilityStatus, DriverProfile

        now = timezone.now()
        with transaction.atomic():
            # Row lock serialises with availability changes, so a fix can never
            # resurrect a location for a driver who has just gone offline.
            status = (
                DriverProfile.objects.select_for_update()
                .filter(pk=fix.driver_id)
                .values_list("availability_status", flat=True)
                .first()
            )
            if status is None:
                return WriteResult(WriteStatus.DRIVER_UNKNOWN, None)
            if status != AvailabilityStatus.ONLINE:
                return WriteResult(WriteStatus.DRIVER_OFFLINE, None)
            with connection.cursor() as cursor:
                cursor.execute(
                    self._UPSERT_SQL,
                    [
                        fix.driver_id,
                        fix.latitude,
                        fix.longitude,
                        fix.heading,
                        fix.speed_kmh,
                        fix.accuracy_m,
                        fix.recorded_at,
                        now,
                        fix.h3_cell,
                        fix.h3_resolution,
                    ],
                )
                applied = cursor.fetchone() is not None
        if applied:
            return WriteResult(WriteStatus.APPLIED, replace(fix, updated_at=now))
        return WriteResult(WriteStatus.NOT_NEWER, self.get(fix.driver_id))

    def set_availability(self, driver_id, *, online, category):
        if not online:
            self.remove(driver_id)

    def seed_availability(self, driver_id, *, online, category):
        pass  # PostgreSQL *is* the availability source of truth

    def remove(self, driver_id: int) -> None:
        DriverLocation.objects.filter(driver_id=driver_id).delete()

    def get(self, driver_id: int) -> LocationFix | None:
        row = DriverLocation.objects.filter(driver_id=driver_id).first()
        return _from_model(row) if row else None

    def _online(self, qs):
        from apps.drivers.models import AvailabilityStatus

        return qs.filter(driver__availability_status=AvailabilityStatus.ONLINE)

    def find_in_bbox(self, bbox: BoundingBox, *, fresh_after: datetime) -> list[LocationFix]:
        rows = self._online(
            DriverLocation.objects.filter(
                latitude__gte=bbox.min_lat,
                latitude__lte=bbox.max_lat,
                longitude__gte=bbox.min_lng,
                longitude__lte=bbox.max_lng,
                recorded_at__gte=fresh_after,
            )
        )
        return [_from_model(row) for row in rows]

    def find_in_cells(self, cells: list[str], *, fresh_after: datetime) -> list[LocationFix]:
        if not cells:
            return []
        rows = self._online(
            DriverLocation.objects.filter(h3_cell__in=cells, recorded_at__gte=fresh_after)
        )
        return [_from_model(row) for row in rows]

    def last_history_point(self, driver_id: int) -> HistoryPoint | None:
        last = (
            DriverLocationEvent.objects.filter(driver_id=driver_id)
            .order_by("-recorded_at")
            .values_list("latitude", "longitude", "recorded_at")
            .first()
        )
        return HistoryPoint(*last) if last else None

    def mark_history_point(self, driver_id: int, point: HistoryPoint) -> None:
        pass  # the DriverLocationEvent row itself is the marker

    def reindex_cells(self, cell_fn: CellFunction) -> tuple[int, int]:
        updated = failed = 0
        for row in DriverLocation.objects.iterator():
            try:
                cell, resolution = cell_fn(float(row.latitude), float(row.longitude))
            except ValueError:
                failed += 1
                continue
            if (row.h3_cell, row.h3_resolution) == (cell, resolution):
                continue
            # Conditional: don't clobber a newer fix written meanwhile.
            updated += DriverLocation.objects.filter(pk=row.pk, recorded_at=row.recorded_at).update(
                h3_cell=cell, h3_resolution=resolution
            )
        return updated, failed

    def prune(self, *, older_than: datetime) -> int:
        return DriverLocation.objects.filter(recorded_at__lt=older_than).delete()[0]

    def all_fixes(self) -> list[LocationFix]:
        """Used by failover recovery to replay fallback writes into the primary."""
        return [_from_model(row) for row in DriverLocation.objects.all()]

    def health(self) -> dict:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except Exception as exc:  # noqa: BLE001
            return {"status": "unavailable", "error": type(exc).__name__}
        return {"status": "ok"}
