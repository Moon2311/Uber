"""Redis primary + PostgreSQL fallback, with a simple circuit breaker.

CLOSED (normal)  : every call goes to the primary (Redis).
OPEN (outage)    : a primary call raised StoreUnavailable. For the next
                   LOCATION_STORE_CIRCUIT_COOLDOWN_SECONDS every call goes straight to
                   the fallback (PostgreSQL) without waiting on Redis timeouts.
HALF-OPEN        : after the cooldown the next call tries Redis again. On success
                   the circuit closes and recovery runs (below); on failure it reopens.

Recovery (first successful primary call after an outage):
  1. `on_recovery(since)` re-pushes driver availability changed during the outage
     from PostgreSQL (drivers.services.resync_live_availability). `since` is the
     detected outage start minus `resync_margin_seconds`: the change whose mirror
     write *triggered* detection was committed just before detection, and other
     processes/servers may have clock skew. Resync is idempotent, so a generous
     margin costs only a few extra writes.
  2. Fixes written to the fallback table during the outage are replayed into Redis
     with upsert_if_newer (newer-wins, so replay is idempotent and never regresses).
  3. The fallback table is emptied (live locations must not linger in PostgreSQL).

Each process has its own breaker, so in-process recovery only runs in a process that
saw the outage. `reconcile()` (manage.py reconcile_live_store: at startup and every
minute) is the process-independent path: it replays any fallback rows and, if Redis
lost its keyspace (epoch marker missing), re-pushes availability for every driver.
All steps are idempotent so concurrent runs are safe.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from django.utils import timezone

from apps.common.metrics import metrics

from .stores import LocationStore, PostgresLocationStore, StoreUnavailable

logger = logging.getLogger("apps.locations")


class FailoverLocationStore(LocationStore):
    name = "failover"

    def __init__(
        self,
        primary: LocationStore,
        fallback: PostgresLocationStore,
        *,
        cooldown_seconds: float,
        on_recovery: Callable[[datetime], None] | None = None,
        resync_margin_seconds: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.primary = primary
        self.fallback = fallback
        self.cooldown = cooldown_seconds
        self.on_recovery = on_recovery
        self.resync_margin = timedelta(seconds=resync_margin_seconds)
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._open_until: float | None = None
        self._outage_started_at: datetime | None = None

    # --- breaker -------------------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._open_until is not None and self._monotonic() < self._open_until

    @property
    def degraded(self) -> bool:
        return self._outage_started_at is not None

    def _trip(self, op: str, exc: Exception) -> None:
        with self._lock:
            first = self._outage_started_at is None
            if first:
                self._outage_started_at = timezone.now()
            self._open_until = self._monotonic() + self.cooldown
        metrics().increment("location.store.failover", tags={"op": op})
        log = logger.error if first else logger.warning
        log("location.store.primary_unavailable", extra={"op": op, "error": str(exc)})

    def _recovered(self) -> None:
        with self._lock:
            started, self._outage_started_at, self._open_until = (
                self._outage_started_at,
                None,
                None,
            )
        if started is None:
            return
        logger.warning("location.store.primary_recovered", extra={"outage_started": started})
        metrics().increment("location.store.recovered")
        try:
            self.recover(outage_started_at=started)
        except StoreUnavailable as exc:
            self._trip("recover", exc)

    def recover(self, *, outage_started_at: datetime | None) -> int:
        """outage_started_at=None means "unknown": resync every driver."""
        if self.on_recovery is not None:
            since = None if outage_started_at is None else outage_started_at - self.resync_margin
            self.on_recovery(since)
        replayed = 0
        for fix in self.fallback.all_fixes():
            replayed += self.primary.upsert_if_newer(fix).applied
            self.fallback.remove(fix.driver_id)
        metrics().gauge("location.store.recovery_replayed", replayed)
        return replayed

    def reconcile(self) -> dict:
        """Process-independent recovery. Raises StoreUnavailable if Redis is down."""
        keyspace_lost = self.primary.ensure_epoch()
        pending = self.fallback.all_fixes()
        if not keyspace_lost and not pending:
            return {"action": "none", "replayed": 0}
        if keyspace_lost:
            since = None  # everything in Redis is gone: resync all drivers
        else:
            oldest = min(f.updated_at or f.recorded_at for f in pending)
            since = oldest
        replayed = self.recover(outage_started_at=since)
        metrics().increment("location.store.reconciled", tags={"keyspace_lost": keyspace_lost})
        logger.warning(
            "location.store.reconciled",
            extra={"keyspace_lost": keyspace_lost, "replayed": replayed},
        )
        return {
            "action": "resync_all" if keyspace_lost else "replay",
            "replayed": replayed,
        }

    def _call(self, op: str, *args, **kwargs):
        if not self.is_open:
            try:
                result = getattr(self.primary, op)(*args, **kwargs)
            except StoreUnavailable as exc:
                self._trip(op, exc)
            else:
                if self.degraded:
                    self._recovered()
                return result
        metrics().increment("location.store.fallback_call", tags={"op": op})
        return getattr(self.fallback, op)(*args, **kwargs)

    # --- LocationStore -------------------------------------------------------------
    def upsert_if_newer(self, fix):
        return self._call("upsert_if_newer", fix)

    def set_availability(self, driver_id, *, online, category):
        self._call("set_availability", driver_id, online=online, category=category)
        if not online:
            # Privacy: an offline driver's position must not survive in either store.
            self.fallback.remove(driver_id)

    def seed_availability(self, driver_id, *, online, category):
        self._call("seed_availability", driver_id, online=online, category=category)

    def remove(self, driver_id):
        self._call("remove", driver_id)
        self.fallback.remove(driver_id)

    def get(self, driver_id):
        return self._call("get", driver_id)

    def find_in_bbox(self, bbox, *, fresh_after):
        return self._call("find_in_bbox", bbox, fresh_after=fresh_after)

    def find_in_cells(self, cells, *, fresh_after):
        return self._call("find_in_cells", cells, fresh_after=fresh_after)

    def last_history_point(self, driver_id):
        return self._call("last_history_point", driver_id)

    def mark_history_point(self, driver_id, point):
        return self._call("mark_history_point", driver_id, point)

    def reindex_cells(self, cell_fn):
        primary = self.primary.reindex_cells(cell_fn)  # maintenance: no silent fallback
        fallback = self.fallback.reindex_cells(cell_fn)
        return primary[0] + fallback[0], primary[1] + fallback[1]

    def prune(self, *, older_than):
        return self.primary.prune(older_than=older_than) + self.fallback.prune(
            older_than=older_than
        )

    def health(self):
        primary = self.primary.health()
        return {
            "status": "ok" if primary["status"] == "ok" else "degraded",
            "primary": {"backend": self.primary.name, **primary},
            "fallback": {"backend": self.fallback.name},
            "circuit_open": self.is_open,
        }
