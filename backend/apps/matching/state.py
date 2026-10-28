"""Driver State Store: eligibility, availability, active trips and reservations.

Two layers, deliberately:
  * PostgreSQL (authoritative): availability_status, active vehicle + category,
    rating, active trip, PENDING offer (= reservation). Offer creation re-checks all
    of it under row locks and DB constraints, so correctness never depends on Redis.
  * Redis reservation locks (advisory, fast): SET NX PX per driver so that many
    concurrent matchers do not all pile onto the same best driver and then lose in
    PostgreSQL. If Redis is down the lock layer reports "unknown" and matching
    proceeds on PostgreSQL alone (slower under contention, still correct).
"""

from __future__ import annotations

import enum
import logging
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.common.metrics import metrics
from apps.drivers.models import AvailabilityStatus, DriverProfile, Vehicle
from apps.trips.models import DRIVER_ACTIVE_STATUSES, Trip

from .discovery import RawCandidate
from .models import OfferStatus, RideOffer

logger = logging.getLogger("apps.matching")


class Rejection(str, enum.Enum):
    STALE = "stale"  # location too old
    OFFLINE = "offline"
    BUSY = "busy"  # on an active trip
    RESERVED = "reserved"  # holds a pending offer / reservation lock for another trip
    INCOMPATIBLE = "incompatible"  # vehicle category cannot serve the ride category
    INELIGIBLE = "ineligible"  # no active vehicle, rating below threshold, unknown driver
    EXCLUDED = "excluded"  # already offered this trip (declined / expired)


@dataclass(frozen=True)
class EligibleDriver:
    raw: RawCandidate
    public_id: str
    vehicle_category: str
    rating: float

    @property
    def driver_id(self) -> int:
        return self.raw.driver_id


# --- reservation locks ------------------------------------------------------------------
class ReservationLocks(ABC):
    @abstractmethod
    def try_acquire(self, driver_id: int, trip_id: str, ttl_seconds: float) -> bool | None:
        """True = acquired (or already ours), False = held for another trip,
        None = lock service unavailable (caller falls back to PostgreSQL alone)."""

    @abstractmethod
    def release(self, driver_id: int, trip_id: str) -> None:
        """Release only if held for `trip_id` (never someone else's lock)."""

    @abstractmethod
    def holders(self, driver_ids: list[int]) -> dict[int, str]:
        """driver_id -> trip_id for currently held locks ({} if unavailable)."""


class NullReservationLocks(ReservationLocks):
    def try_acquire(self, driver_id, trip_id, ttl_seconds):
        return None

    def release(self, driver_id, trip_id):
        pass

    def holders(self, driver_ids):
        return {}


_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
_ACQUIRE_LUA = """
local cur = redis.call('GET', KEYS[1])
if cur == false then
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2]); return 1
end
if cur == ARGV[1] then redis.call('PEXPIRE', KEYS[1], ARGV[2]); return 1 end
return 0
"""


class RedisReservationLocks(ReservationLocks):
    def __init__(self, client=None, prefix: str | None = None):
        from apps.common.redis import get_redis

        self.r = client or get_redis()
        self.prefix = f"{prefix or settings.REDIS_KEY_PREFIX}:{{match}}:resv"
        self._acquire = self.r.register_script(_ACQUIRE_LUA)
        self._release = self.r.register_script(_RELEASE_LUA)

    def _key(self, driver_id) -> str:
        return f"{self.prefix}:{driver_id}"

    def _guard(self, fn, default):
        import redis

        try:
            return fn()
        except redis.exceptions.RedisError as exc:
            metrics().increment("matching.reservation_lock.unavailable")
            logger.warning("matching.reservation_lock.unavailable", extra={"error": str(exc)})
            return default

    def try_acquire(self, driver_id, trip_id, ttl_seconds):
        return self._guard(
            lambda: bool(
                self._acquire(keys=[self._key(driver_id)], args=[trip_id, int(ttl_seconds * 1000)])
            ),
            None,
        )

    def release(self, driver_id, trip_id):
        self._guard(lambda: self._release(keys=[self._key(driver_id)], args=[trip_id]), None)

    def holders(self, driver_ids):
        if not driver_ids:
            return {}

        def read():
            values = self.r.mget([self._key(d) for d in driver_ids])
            return {d: v for d, v in zip(driver_ids, values, strict=True) if v}

        return self._guard(read, {})


def get_reservation_locks() -> ReservationLocks:
    if settings.MATCHING_RESERVATION_LOCKS == "redis":
        return RedisReservationLocks()
    return NullReservationLocks()


# --- state store ----------------------------------------------------------------------
class DriverStateStore:
    def __init__(self, locks: ReservationLocks | None = None, clock=timezone.now):
        self.locks = locks or get_reservation_locks()
        self.clock = clock

    def filter_eligible(
        self,
        raws: list[RawCandidate],
        *,
        ride_category: str,
        trip_id: str,
        exclude: set[int],
    ) -> tuple[list[EligibleDriver], Counter]:
        rejected: Counter = Counter()
        if not raws:
            return [], rejected
        fresh_after = self.clock() - timedelta(seconds=settings.DRIVER_LOCATION_STALE_SECONDS)
        compatible = set(settings.MATCHING_CATEGORY_COMPATIBILITY.get(ride_category, []))
        ids = [r.driver_id for r in raws]

        rows = {
            row["pk"]: row
            for row in DriverProfile.objects.filter(pk__in=ids)
            .annotate(
                on_trip=Exists(
                    Trip.objects.filter(driver=OuterRef("pk"), status__in=DRIVER_ACTIVE_STATUSES)
                ),
                pending_offer_trip=RideOffer.objects.filter(
                    driver=OuterRef("pk"), status=OfferStatus.PENDING
                ).values("trip_id")[:1],
            )
            .values(
                "pk", "public_id", "availability_status", "rating", "on_trip", "pending_offer_trip"
            )  # fmt: skip
        }
        vehicles = dict(
            Vehicle.objects.filter(driver_id__in=ids, is_active=True).values_list(
                "driver_id", "category"
            )
        )
        locked = self.locks.holders(ids)

        eligible = []
        for raw in raws:
            row = rows.get(raw.driver_id)
            reason = self._rejection(
                raw, row, vehicles.get(raw.driver_id), locked.get(raw.driver_id),
                trip_id=str(trip_id), exclude=exclude, fresh_after=fresh_after,
                compatible=compatible,
            )  # fmt: skip
            if reason is not None:
                rejected[reason.value] += 1
                continue
            eligible.append(
                EligibleDriver(
                    raw=raw,
                    public_id=str(row["public_id"]),
                    vehicle_category=vehicles[raw.driver_id],
                    rating=float(row["rating"]),
                )
            )
        for reason, count in rejected.items():
            metrics().increment("matching.candidates.rejected", count, tags={"reason": reason})
        return eligible, rejected

    @staticmethod
    def _rejection(raw, row, vehicle_category, lock_holder, *, trip_id, exclude, fresh_after,
                   compatible) -> Rejection | None:  # fmt: skip
        if raw.driver_id in exclude:
            return Rejection.EXCLUDED
        if row is None or vehicle_category is None:
            return Rejection.INELIGIBLE
        if float(row["rating"]) < settings.MATCHING_MIN_DRIVER_RATING:
            return Rejection.INELIGIBLE
        if row["availability_status"] != AvailabilityStatus.ONLINE:
            return Rejection.OFFLINE
        if raw.fix.recorded_at < fresh_after:
            return Rejection.STALE
        if row["on_trip"]:
            return Rejection.BUSY
        if row["pending_offer_trip"] is not None and str(row["pending_offer_trip"]) != trip_id:
            return Rejection.RESERVED
        if lock_holder is not None and lock_holder != trip_id:
            return Rejection.RESERVED
        if vehicle_category not in compatible:
            return Rejection.INCOMPATIBLE
        return None
