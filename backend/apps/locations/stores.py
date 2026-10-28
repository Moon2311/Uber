"""Storage layer for *latest* driver locations.

`LocationStore` is the seam that lets PostgreSQL be replaced (or fronted) by Redis
in a later step. Implementations must honour these semantics:

* `upsert_if_newer` is atomic: the stored fix is replaced only when the incoming
  `recorded_at` is strictly newer. Equal or older timestamps are no-ops.
* `find_in_bbox` returns only fixes with `recorded_at >= fresh_after`.
* The store knows nothing about driver availability, vehicles or trips; that
  filtering belongs to the drivers domain.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.db import connection
from django.utils import timezone

from .models import DriverLocation


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
class BoundingBox:
    min_lat: float
    max_lat: float
    min_lng: float
    max_lng: float


class LocationStore(ABC):
    @abstractmethod
    def upsert_if_newer(self, fix: LocationFix) -> tuple[bool, LocationFix]:
        """Store `fix` if newer than the current one.

        Returns (applied, current_fix) where current_fix is what is stored afterwards.
        """

    @abstractmethod
    def get(self, driver_id: int) -> LocationFix | None: ...

    @abstractmethod
    def remove(self, driver_id: int) -> None: ...

    @abstractmethod
    def find_in_bbox(self, bbox: BoundingBox, *, fresh_after: datetime) -> list[LocationFix]: ...

    @abstractmethod
    def find_in_cells(self, cells: list[str], *, fresh_after: datetime) -> list[LocationFix]:
        """Fixes whose stored H3 cell is one of `cells` and recorded_at >= fresh_after."""


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
    """One row per driver in `locations_driverlocation`, written with a guarded UPSERT."""

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

    def upsert_if_newer(self, fix: LocationFix) -> tuple[bool, LocationFix]:
        now = timezone.now()
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
            return True, LocationFix(**{**fix.__dict__, "updated_at": now})
        return False, self.get(fix.driver_id)

    def get(self, driver_id: int) -> LocationFix | None:
        row = DriverLocation.objects.filter(driver_id=driver_id).first()
        return _from_model(row) if row else None

    def remove(self, driver_id: int) -> None:
        DriverLocation.objects.filter(driver_id=driver_id).delete()

    def find_in_bbox(self, bbox: BoundingBox, *, fresh_after: datetime) -> list[LocationFix]:
        rows = DriverLocation.objects.filter(
            latitude__gte=bbox.min_lat,
            latitude__lte=bbox.max_lat,
            longitude__gte=bbox.min_lng,
            longitude__lte=bbox.max_lng,
            recorded_at__gte=fresh_after,
        )
        return [_from_model(row) for row in rows]

    def find_in_cells(self, cells: list[str], *, fresh_after: datetime) -> list[LocationFix]:
        if not cells:
            return []
        rows = DriverLocation.objects.filter(h3_cell__in=cells, recorded_at__gte=fresh_after)
        return [_from_model(row) for row in rows]
