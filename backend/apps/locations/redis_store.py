"""Redis hot store for live driver state.

Key layout (P = "<REDIS_KEY_PREFIX>:{live}"; the {live} hash tag puts every key in
one cluster slot so Lua scripts can touch several keys):

  P:drv:<id>        HASH  status, category, lat, lng, heading, speed, acc,
                          ts (recorded_at, epoch µs), upd (server µs), cell, res,
                          hlat, hlng, hts (last history sample)
                          TTL = LIVE_DRIVER_TTL_SECONDS, refreshed on every write
  P:cell:<h3>       ZSET  member=driver id, score=ts       TTL refreshed on write
  P:lastcell        HASH  driver id -> cell (lets pruning find a member after the
                          driver hash has expired)
  P:seen            ZSET  member=driver id, score=ts (staleness sweeps)
  P:geo             GEO   driver positions (bbox fallback search)
  P:epoch           STRING random id set once per keyspace lifetime; its absence
                          means Redis lost its data (restart without persistence,
                          flush, failover to an empty replica) -> reconcile

Freshness = `ts >= now - DRIVER_LOCATION_STALE_SECONDS`, checked on every read. TTLs
only reclaim memory; correctness never depends on a key having expired on time.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import redis
from django.conf import settings
from django.utils import timezone as dj_tz

from apps.common.geo import haversine_km
from apps.common.metrics import metrics
from apps.common.redis import get_redis, is_unavailable_error, redis_health

from .stores import (
    BoundingBox,
    CellFunction,
    HistoryPoint,
    LocationFix,
    LocationStore,
    StoreUnavailable,
    WriteResult,
    WriteStatus,
)

MAX_CAS_ATTEMPTS = 5
GEO_MAX_LAT = 85.05112878  # Redis GEO cannot index beyond this latitude

# Shared Lua helper: remove a driver's location + every index entry. Caller has
# already verified the expected cell. KEYS: drv, seen, geo, lastcell, oldcell
_CLEAR_LOCATION_LUA = """
local function clear_location(id, cur_cell)
  if cur_cell ~= '' then redis.call('ZREM', KEYS[5], id) end
  redis.call('ZREM', KEYS[2], id)
  redis.call('ZREM', KEYS[3], id)
  redis.call('HDEL', KEYS[4], id)
  redis.call('HDEL', KEYS[1], 'lat', 'lng', 'heading', 'speed', 'acc', 'ts', 'upd',
             'cell', 'res', 'hlat', 'hlng', 'hts')
end
"""

# KEYS: 1 drv, 2 seen, 3 geo, 4 lastcell, 5 new cell zset, 6 old cell zset
# ARGV: 1 id, 2 ts, 3 lat, 4 lng, 5 heading, 6 speed, 7 acc, 8 upd, 9 new cell,
#       10 res, 11 expected current cell, 12 driver ttl, 13 cell ttl, 14 prune-before ts
UPSERT_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
if not status then return 'UNKNOWN' end
if status ~= 'ONLINE' then return 'OFFLINE' end
local cur_cell = redis.call('HGET', KEYS[1], 'cell') or ''
if cur_cell ~= ARGV[11] then return 'RETRY' end
local cur_ts = redis.call('HGET', KEYS[1], 'ts')
if cur_ts and tonumber(cur_ts) >= tonumber(ARGV[2]) then return 'NOT_NEWER' end
-- all checks passed; writes below cannot fail on valid input
redis.call('HSET', KEYS[1], 'lat', ARGV[3], 'lng', ARGV[4], 'heading', ARGV[5],
           'speed', ARGV[6], 'acc', ARGV[7], 'ts', ARGV[2], 'upd', ARGV[8],
           'cell', ARGV[9], 'res', ARGV[10])
redis.call('EXPIRE', KEYS[1], ARGV[12])
if cur_cell ~= '' and cur_cell ~= ARGV[9] then redis.call('ZREM', KEYS[6], ARGV[1]) end
if ARGV[9] ~= '' then
  redis.call('ZADD', KEYS[5], ARGV[2], ARGV[1])
  redis.call('ZREMRANGEBYSCORE', KEYS[5], '-inf', '(' .. ARGV[14])
  redis.call('EXPIRE', KEYS[5], ARGV[13])
  redis.call('HSET', KEYS[4], ARGV[1], ARGV[9])
else
  redis.call('HDEL', KEYS[4], ARGV[1])
end
redis.call('ZADD', KEYS[2], ARGV[2], ARGV[1])
local lat = tonumber(ARGV[3])
if lat <= GEO_MAX and lat >= -GEO_MAX then
  redis.call('GEOADD', KEYS[3], ARGV[4], ARGV[3], ARGV[1])
else
  redis.call('ZREM', KEYS[3], ARGV[1])
end
return 'APPLIED'
""".replace("GEO_MAX", str(GEO_MAX_LAT))

# KEYS: 1 drv, 2 seen, 3 geo, 4 lastcell, 5 current cell zset
# ARGV: 1 id, 2 mode (online|offline|seed_online|seed_offline|clear), 3 category,
#       4 driver ttl, 5 expected current cell
AVAILABILITY_LUA = (
    _CLEAR_LOCATION_LUA
    + """
local mode = ARGV[2]
local status = redis.call('HGET', KEYS[1], 'status')
if string.sub(mode, 1, 5) == 'seed_' and status then return 'EXISTS' end
local cur_cell = redis.call('HGET', KEYS[1], 'cell') or ''
if cur_cell ~= ARGV[5] then return 'RETRY' end
if mode == 'clear' then
  clear_location(ARGV[1], cur_cell)
  return 'OK'
end
local target = 'OFFLINE'
if mode == 'online' or mode == 'seed_online' then target = 'ONLINE' end
if target == 'OFFLINE' then clear_location(ARGV[1], cur_cell) end
redis.call('HSET', KEYS[1], 'status', target, 'category', ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 'OK'
"""
)

# Move a driver to a recomputed cell without changing the fix (resolution change).
# KEYS: 1 drv, 2 lastcell, 3 new cell zset, 4 old cell zset
# ARGV: 1 id, 2 expected ts, 3 expected cell, 4 new cell, 5 res, 6 cell ttl
REINDEX_LUA = """
local ts = redis.call('HGET', KEYS[1], 'ts')
if not ts or ts ~= ARGV[2] then return 'CHANGED' end
local cur_cell = redis.call('HGET', KEYS[1], 'cell') or ''
if cur_cell ~= ARGV[3] then return 'CHANGED' end
if cur_cell == ARGV[4] then return 'SAME' end
if cur_cell ~= '' then redis.call('ZREM', KEYS[4], ARGV[1]) end
redis.call('ZADD', KEYS[3], ts, ARGV[1])
redis.call('EXPIRE', KEYS[3], ARGV[6])
redis.call('HSET', KEYS[1], 'cell', ARGV[4], 'res', ARGV[5])
redis.call('HSET', KEYS[2], ARGV[1], ARGV[4])
return 'OK'
"""

# Remove an expired driver from every index. Works even if the driver hash has
# already expired (cell comes from P:lastcell).
# KEYS: 1 drv, 2 seen, 3 geo, 4 lastcell, 5 cell zset   ARGV: 1 id, 2 cutoff ts, 3 cell
PRUNE_LUA = """
local score = redis.call('ZSCORE', KEYS[2], ARGV[1])
if score and tonumber(score) >= tonumber(ARGV[2]) then return 'FRESH' end
if (redis.call('HGET', KEYS[4], ARGV[1]) or '') ~= ARGV[3] then return 'RETRY' end
if ARGV[3] ~= '' then redis.call('ZREM', KEYS[5], ARGV[1]) end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('HDEL', KEYS[4], ARGV[1])
local ts = redis.call('HGET', KEYS[1], 'ts')
if ts and tonumber(ts) < tonumber(ARGV[2]) then
  redis.call('HDEL', KEYS[1], 'lat', 'lng', 'heading', 'speed', 'acc', 'ts', 'upd',
             'cell', 'res', 'hlat', 'hlng', 'hts')
end
return 'PRUNED'
"""

# Set the history marker only on a live hash (never create a TTL-less hash).
# KEYS: 1 drv   ARGV: 1 lat, 2 lng, 3 ts
MARK_HISTORY_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
redis.call('HSET', KEYS[1], 'hlat', ARGV[1], 'hlng', ARGV[2], 'hts', ARGV[3])
return 1
"""

_FIX_FIELDS = ["status", "lat", "lng", "heading", "speed", "acc", "ts", "upd", "cell", "res"]


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_US = timedelta(microseconds=1)


def _to_us(dt: datetime) -> int:
    """Exact integer microseconds since the epoch (fits a Lua double exactly)."""
    return (dt - _EPOCH) // _US


def _from_us(value: str | int) -> datetime:
    return _EPOCH + int(value) * _US


def _opt_float(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None


def _enc(value) -> str:
    return "" if value is None else str(value)


class RedisLocationStore(LocationStore):
    name = "redis"

    def __init__(self, client: redis.Redis | None = None, prefix: str | None = None):
        self.r = client or get_redis()
        p = f"{prefix or settings.REDIS_KEY_PREFIX}:{{live}}"
        self.k_seen, self.k_geo, self.k_lastcell = f"{p}:seen", f"{p}:geo", f"{p}:lastcell"
        self.k_epoch = f"{p}:epoch"
        self._p = p
        self._upsert = self.r.register_script(UPSERT_LUA)
        self._availability = self.r.register_script(AVAILABILITY_LUA)
        self._reindex = self.r.register_script(REINDEX_LUA)
        self._prune = self.r.register_script(PRUNE_LUA)
        self._mark_history = self.r.register_script(MARK_HISTORY_LUA)

    # --- keys -----------------------------------------------------------------------
    def k_drv(self, driver_id) -> str:
        return f"{self._p}:drv:{driver_id}"

    def k_cell(self, cell: str | None) -> str:
        return f"{self._p}:cell:{cell or ''}"

    @property
    def key_prefix(self) -> str:
        return self._p

    # --- helpers --------------------------------------------------------------------
    def _run(self, fn, *args, **kwargs):
        """Translate connectivity failures into StoreUnavailable (failover trigger)."""
        try:
            return fn(*args, **kwargs)
        except redis.exceptions.RedisError as exc:
            if is_unavailable_error(exc):
                raise StoreUnavailable(f"redis: {type(exc).__name__}: {exc}") from exc
            raise

    def _cas(self, driver_id, attempt_fn) -> str:
        """Optimistic loop: read the current cell, run a script that verifies it."""
        for _ in range(MAX_CAS_ATTEMPTS):
            cell = self._run(self.r.hget, self.k_drv(driver_id), "cell") or ""
            outcome = self._run(attempt_fn, cell)
            if outcome != "RETRY":
                return outcome
            metrics().increment("location.store.cas_retry")
        raise StoreUnavailable(f"driver {driver_id}: too much contention")

    @staticmethod
    def _ttls() -> tuple[int, int]:
        ttl = max(1, settings.LIVE_DRIVER_TTL_SECONDS)
        return ttl, ttl

    def _fix_from_hash(self, driver_id: int, h: dict | list) -> LocationFix | None:
        if isinstance(h, list):
            h = dict(zip(_FIX_FIELDS, h, strict=True))
        if not h or not h.get("ts"):
            return None
        return LocationFix(
            driver_id=int(driver_id),
            latitude=Decimal(h["lat"]),
            longitude=Decimal(h["lng"]),
            recorded_at=_from_us(h["ts"]),
            heading=_opt_float(h.get("heading")),
            speed_kmh=_opt_float(h.get("speed")),
            accuracy_m=_opt_float(h.get("acc")),
            updated_at=_from_us(h["upd"]) if h.get("upd") else None,
            h3_cell=h.get("cell") or None,
            h3_resolution=int(h["res"]) if h.get("res") else None,
        )

    # --- writes -----------------------------------------------------------------------
    def upsert_if_newer(self, fix: LocationFix) -> WriteResult:
        now = dj_tz.now()
        driver_ttl, cell_ttl = self._ttls()
        ts = _to_us(fix.recorded_at)
        prune_before = _to_us(now) - driver_ttl * 1_000_000

        def attempt(cur_cell):
            return self._upsert(
                keys=[
                    self.k_drv(fix.driver_id),
                    self.k_seen,
                    self.k_geo,
                    self.k_lastcell,
                    self.k_cell(fix.h3_cell),
                    self.k_cell(cur_cell),
                ],
                args=[
                    fix.driver_id,
                    ts,
                    _enc(fix.latitude),
                    _enc(fix.longitude),
                    _enc(fix.heading),
                    _enc(fix.speed_kmh),
                    _enc(fix.accuracy_m),
                    _to_us(now),
                    _enc(fix.h3_cell),
                    _enc(fix.h3_resolution),
                    cur_cell,
                    driver_ttl,
                    cell_ttl,
                    prune_before,
                ],
            )

        outcome = self._cas(fix.driver_id, attempt)
        if outcome == "APPLIED":
            return WriteResult(WriteStatus.APPLIED, replace(fix, updated_at=now))
        status = {
            "UNKNOWN": WriteStatus.DRIVER_UNKNOWN,
            "OFFLINE": WriteStatus.DRIVER_OFFLINE,
            "NOT_NEWER": WriteStatus.NOT_NEWER,
        }[outcome]
        current = self.get(fix.driver_id) if status is WriteStatus.NOT_NEWER else None
        return WriteResult(status, current)

    def _availability_call(self, driver_id, mode, category) -> str:
        driver_ttl, _ = self._ttls()

        def attempt(cur_cell):
            return self._availability(
                keys=[
                    self.k_drv(driver_id),
                    self.k_seen,
                    self.k_geo,
                    self.k_lastcell,
                    self.k_cell(cur_cell),
                ],
                args=[driver_id, mode, _enc(category), driver_ttl, cur_cell],
            )

        return self._cas(driver_id, attempt)

    def set_availability(self, driver_id, *, online, category):
        self._availability_call(driver_id, "online" if online else "offline", category)

    def seed_availability(self, driver_id, *, online, category):
        self._availability_call(driver_id, "seed_online" if online else "seed_offline", category)

    def remove(self, driver_id):
        self._availability_call(driver_id, "clear", None)

    # --- reads ------------------------------------------------------------------------
    def get(self, driver_id):
        values = self._run(self.r.hmget, self.k_drv(driver_id), _FIX_FIELDS)
        return self._fix_from_hash(driver_id, values)

    def _load_fresh(self, driver_ids, fresh_after, *, within_cells=None) -> list[LocationFix]:
        """Fetch hashes and re-verify everything: index entries may lag behind (a
        driver hash can expire before its cell membership is pruned)."""
        if not driver_ids:
            return []
        pipe = self.r.pipeline(transaction=False)
        for driver_id in driver_ids:
            pipe.hmget(self.k_drv(driver_id), _FIX_FIELDS)
        rows = self._run(pipe.execute)
        fresh_us = _to_us(fresh_after)
        out = []
        for driver_id, values in zip(driver_ids, rows, strict=True):
            h = dict(zip(_FIX_FIELDS, values, strict=True))
            if h.get("status") != "ONLINE" or not h.get("ts") or int(h["ts"]) < fresh_us:
                continue
            if within_cells is not None and h.get("cell") not in within_cells:
                continue  # stale membership: the driver has moved elsewhere
            out.append(self._fix_from_hash(driver_id, h))
        if len(out) < len(driver_ids):
            metrics().increment("location.store.filtered_stale", len(driver_ids) - len(out))
        return out

    def find_in_cells(self, cells, *, fresh_after):
        if not cells:
            return []
        pipe = self.r.pipeline(transaction=False)
        for cell in cells:
            pipe.zrangebyscore(self.k_cell(cell), _to_us(fresh_after), "+inf")
        members = self._run(pipe.execute)
        driver_ids = sorted({int(i) for ids in members for i in ids})
        return self._load_fresh(driver_ids, fresh_after, within_cells=set(cells))

    def find_in_bbox(self, bbox: BoundingBox, *, fresh_after):
        lat = (bbox.min_lat + bbox.max_lat) / 2
        lng = (bbox.min_lng + bbox.max_lng) / 2
        height = haversine_km(bbox.min_lat, lng, bbox.max_lat, lng)
        width = haversine_km(lat, bbox.min_lng, lat, bbox.max_lng)
        lat = max(-GEO_MAX_LAT, min(GEO_MAX_LAT, lat))
        ids = self._run(
            self.r.geosearch,
            self.k_geo,
            longitude=lng,
            latitude=lat,
            width=max(width, 0.001) * 1.02,
            height=max(height, 0.001) * 1.02,
            unit="km",
        )
        fixes = self._load_fresh([int(i) for i in ids], fresh_after)
        return [
            f
            for f in fixes
            if bbox.min_lat <= f.latitude <= bbox.max_lat
            and bbox.min_lng <= f.longitude <= bbox.max_lng
        ]

    # --- history marker -------------------------------------------------------------
    def last_history_point(self, driver_id):
        lat, lng, ts = self._run(self.r.hmget, self.k_drv(driver_id), ["hlat", "hlng", "hts"])
        if not ts:
            return None
        return HistoryPoint(Decimal(lat), Decimal(lng), _from_us(ts))

    def mark_history_point(self, driver_id, point):
        self._run(
            self._mark_history,
            keys=[self.k_drv(driver_id)],
            args=[str(point.latitude), str(point.longitude), _to_us(point.recorded_at)],
        )

    # --- maintenance -----------------------------------------------------------------
    def _live_ids(self) -> list[int]:
        return [int(i) for i in self._run(self.r.zrange, self.k_seen, 0, -1)]

    def reindex_cells(self, cell_fn: CellFunction) -> tuple[int, int]:
        _, cell_ttl = self._ttls()
        updated = failed = 0
        for driver_id in self._live_ids():
            values = self._run(self.r.hmget, self.k_drv(driver_id), ["lat", "lng", "ts", "cell"])
            lat, lng, ts, cur = values
            if not ts:
                continue
            try:
                cell, resolution = cell_fn(float(lat), float(lng))
            except ValueError:
                failed += 1
                continue
            outcome = self._run(
                self._reindex,
                keys=[
                    self.k_drv(driver_id),
                    self.k_lastcell,
                    self.k_cell(cell),
                    self.k_cell(cur),
                ],
                args=[driver_id, ts, cur or "", cell, resolution, cell_ttl],
            )
            updated += outcome == "OK"
        return updated, failed

    def prune(self, *, older_than):
        cutoff = _to_us(older_than)
        expired = self._run(self.r.zrangebyscore, self.k_seen, "-inf", f"({cutoff}")
        pruned = 0
        for driver_id in expired:
            for _ in range(MAX_CAS_ATTEMPTS):
                cell = self._run(self.r.hget, self.k_lastcell, driver_id) or ""
                outcome = self._run(
                    self._prune,
                    keys=[
                        self.k_drv(driver_id),
                        self.k_seen,
                        self.k_geo,
                        self.k_lastcell,
                        self.k_cell(cell),
                    ],
                    args=[driver_id, cutoff, cell],
                )
                if outcome != "RETRY":
                    pruned += outcome == "PRUNED"
                    break
        return pruned

    def ensure_epoch(self) -> bool:
        """Create the keyspace epoch marker if absent. True = keyspace was (re)created,
        i.e. live data was lost and must be reconciled from PostgreSQL."""
        import uuid

        return bool(self._run(self.r.set, self.k_epoch, uuid.uuid4().hex, nx=True))

    def health(self):
        return redis_health(self.r)
