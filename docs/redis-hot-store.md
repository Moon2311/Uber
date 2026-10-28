# Redis Hot Store (Step 4)

Redis now holds the **live** driver state that changes every few seconds: the
latest GPS fix, a mirror of availability, and H3 cell membership. PostgreSQL is
still the durable source of truth for every application record. Redis contents
are disposable: the system tolerates losing all of it, and this document explains
how each piece comes back.

## What lives where

| Data | Where | Durable? | If Redis loses it |
|---|---|---|---|
| Users, driver profiles, vehicles, trips, payments | PostgreSQL | yes | not affected |
| Driver availability (`availability_status`) | **PostgreSQL** (source of truth) + Redis mirror | yes | mirror rebuilt from PostgreSQL: on the driver's next fix (seed), by `reconcile_live_store`, or by `rebuild_live_index` |
| Sampled location history (`DriverLocationEvent`) | PostgreSQL | yes, 30-day retention | not affected |
| Latest GPS fix per driver | Redis | **no** | **reappears with each online driver's next GPS fix** (normally within seconds). It is not kept in PostgreSQL in normal operation. |
| H3 cell → drivers, geo index, "last seen" index | Redis | no | **derived** from the latest fixes; rebuilt as fixes arrive |
| History sampling marker (last sampled point) | Redis | no | at most one extra history sample per driver |
| Fixes written while Redis was down | PostgreSQL `locations_driverlocation` (fallback) | until replayed | replayed into Redis, then deleted |

Redis in Compose runs with **no persistence** (`--save "" --appendonly no`).
Because every Redis key can be rebuilt, persistence would only add disk I/O and
restart time. Redis also runs with `maxmemory-policy noeviction`: when memory is
full, writes fail loudly and the app fails over to PostgreSQL, instead of Redis
silently evicting index keys.

## Abstraction

```
drivers.candidates  ──► LocationService ──► LocationStore (interface, stores.py)
(matching engine)                            ├─ RedisLocationStore    (redis_store.py)
                                             ├─ PostgresLocationStore (stores.py)
                                             └─ FailoverLocationStore (failover.py)
```

Only `apps/locations/redis_store.py` and `apps/common/redis.py` import `redis`.
The matching and candidate code calls `LocationService.find_in_cells(...)` and
gets `LocationFix` objects back. The store is chosen by `LOCATION_STORE` and
`LOCATION_STORE_FALLBACK`.

The contract suite (`tests/test_store.py`) runs against all three
implementations. The location API, driver and candidate test modules run once
with PostgreSQL and once with Redis. The Redis run has **no** fallback, so a
Redis bug can't hide behind PostgreSQL.

## Key layout

`P` = `<REDIS_KEY_PREFIX>:{live}`. The `{live}` hash tag puts every key in one
Redis Cluster slot, which is required because Lua scripts touch several keys.
Sharding (for example per city) comes with a later step.

| Key | Type | Contents | Expiry |
|---|---|---|---|
| `P:drv:<id>` | HASH | `status`, `category`, `lat`, `lng`, `heading`, `speed`, `acc`, `ts` (fix time, epoch µs), `upd` (server µs), `cell`, `res`, `hlat`/`hlng`/`hts` (history marker) | `LIVE_DRIVER_TTL_SECONDS` (default 900 s) after the last write |
| `P:cell:<h3>` | ZSET | driver ids, score = `ts` | same TTL, refreshed on each add; members older than the TTL are trimmed on each write |
| `P:lastcell` | HASH | driver id → cell, so pruning can find a member after its driver hash expired | entries removed by prune and offline |
| `P:seen` | ZSET | driver ids, score = `ts` | entries removed by prune and offline |
| `P:geo` | GEO | positions, for the bbox fallback strategy (only \|lat\| ≤ 85.05°) | entries removed by prune and offline |
| `P:epoch` | STRING | random id set once per keyspace lifetime | none; its absence means Redis lost its data |

Timestamps are exact integer microseconds; doubles hold them exactly until the
year 2255.

## Atomicity: Lua scripts

Every write that touches more than one key is a single Lua script, so readers
never see a half-applied move.

| Script | Does |
|---|---|
| `UPSERT_LUA` | Checks, in order: driver status present? (else `UNKNOWN`), status `ONLINE`? (else `OFFLINE`), cell still the one the client read? (else `RETRY`), `ts` strictly newer? (else `NOT_NEWER`). Only then: write hash and TTL, `ZREM` from the old cell, `ZADD` to the new cell, trim the cell, update `lastcell`/`seen`/`geo`. |
| `AVAILABILITY_LUA` | `online` / `offline` / `seed_*` / `clear`. Going offline deletes the location and **every** index entry in the same step as the status change. |
| `REINDEX_LUA` | Moves a driver to a recomputed cell (H3 resolution change), only if the fix hasn't changed since it was read. |
| `PRUNE_LUA` | Removes an expired driver from all indexes, unless it has reported again meanwhile. |
| `MARK_HISTORY_LUA` | Sets the history marker only on an existing hash, so it never creates a hash without a TTL. |

**Cell moves.** Redis Cluster requires a script to declare every key it touches,
so the old cell's key must be passed in. The client reads the driver's current
cell (`HGET`) and passes that cell's key plus the expected cell id. If another
writer moved the driver in between, the script answers `RETRY` and the client
re-reads, up to 5 times (`location.store.cas_retry` metric). A test forces this
race. A concurrency test with 8 threads writing 40 shuffled fixes across 19 cells
checks two things: the newest fix wins, and the driver ends up in **exactly one**
cell.

**All checks before any write.** Redis does not roll back a script that fails
halfway. So every validation happens before the first write, and the one write
that can reject input (`GEOADD` above 85.05° latitude) is guarded.

## Idempotency

- **Same fix again:** `NOT_NEWER`, and nothing changes (a test compares the hash
  byte for byte). Client retries are safe.
- **Availability:** `online`/`offline` called repeatedly gives the same result.
- **Seeding:** `seed_*` only writes when no status exists, so self-healing can
  never override a real toggle.
- **Replay and resync** after an outage use newer-wins writes and authoritative
  per-driver locks, so running them twice changes nothing.

## Staleness, TTLs and expired drivers

Two independent mechanisms protect matching from expired drivers:

1. **Timestamps (correctness).** Every read checks `ts >= now − DRIVER_LOCATION_STALE_SECONDS`
   (120 s). `find_in_cells` reads cell members by score, then loads each driver
   hash and checks all of these:
   - the hash exists (it may have expired by TTL)
   - `status == ONLINE`
   - `ts` is fresh
   - the hash's cell is one of the requested cells (the membership may be stale)

   A driver is returned only if every check passes. Index cleanup lagging behind
   therefore never produces a wrong candidate.
2. **TTLs (memory).**
   - Driver hashes and cell sets expire `LIVE_DRIVER_TTL_SECONDS` after the last
     write.
   - `prune_live_index` (every minute) removes expired ids from the
     cell/seen/geo indexes, using `lastcell` when the driver hash is already gone.

Finally, `drivers.candidates` re-checks the shortlist against **PostgreSQL**:
ONLINE, active vehicle, category, not on a trip. A stale Redis mirror (see
recovery below) can therefore delay a driver's removal from Redis, but it can't
make an offline driver a matching candidate.

When a driver hash expires, its availability marker goes too. On the driver's
next fix the store returns `DRIVER_UNKNOWN`. The service then seeds the status
from PostgreSQL with only-if-absent semantics and retries once.

## Availability mirror ordering

`drivers.services._apply_status` updates PostgreSQL and then writes the Redis
mirror **while still holding the driver's row lock**. Concurrent toggles
therefore reach Redis in the same order as PostgreSQL.

If the PostgreSQL commit then failed, Redis would briefly be ahead of PostgreSQL.
That is safe because of the PostgreSQL re-check in candidate retrieval, and the
next toggle or reconcile repairs it.

## Connection, reconnect and failure behaviour

`apps/common/redis.py` sets up the client:
- **Timeouts:** 0.5 s for socket and connect.
- **Retries:** `Retry(ExponentialBackoff(cap=0.25 s), 2)` on connection and
  timeout errors, so a dropped connection is retried on a fresh one.
- **Stale connections:** `health_check_interval=30` PINGs idle pooled connections
  before reuse, so connections killed by a restart or proxy are replaced
  transparently.
- **Script cache:** `register_script` falls back from `EVALSHA` to `EVAL` when
  Redis has lost its script cache (`NOSCRIPT`).

Tests kill the client's server-side connections and flush the script cache, and
the next operation succeeds both times.

**Which errors count as "unavailable".** Connection, timeout, `LOADING`,
`READONLY` (replica failover), cluster-down and `OOM` errors become
`StoreUnavailable`. Any other Redis error, such as a Lua bug, is raised as is and
**never** triggers failover, so bugs can't hide.

**FailoverLocationStore (circuit breaker)**, used when `LOCATION_STORE_FALLBACK=postgres`:

1. A primary call raises `StoreUnavailable`. The circuit opens for
   `LOCATION_STORE_CIRCUIT_COOLDOWN_SECONDS` (5 s), and the call is served by
   PostgreSQL (`location.store.failover` metric, ERROR log on the first failure).
2. While the circuit is open, calls go straight to PostgreSQL without waiting on
   Redis timeouts. In the live outage test, nearby searches took 30–50 ms.
3. After the cooldown, the next call probes Redis. If the probe fails, the circuit
   reopens.
4. If the probe succeeds, in-process recovery runs:
   - availability changed since (outage start − 60 s) is re-pushed from PostgreSQL;
   - fallback fixes are replayed newer-wins;
   - the fallback table is emptied.

   The 60 s margin is there because the change whose mirror write *revealed* the
   outage was committed just before the outage was detected. A test caught exactly
   that bug.
5. Going offline during an outage also deletes the fallback row immediately, for
   privacy.

With `LOCATION_STORE_FALLBACK=none`, a Redis outage returns
**503 `service_unavailable`** instead.

**Health.** `GET /api/v1/health/` returns `live_store`, including the primary's
status and latency and whether the circuit is open.

| Database | Redis | Fallback | `status` | HTTP |
|---|---|---|---|---|
| ok | ok | — | `ok` | 200 |
| ok | down | `postgres` | `degraded` | **200** (still serving, stays in rotation) |
| ok | down | `none` | `unavailable` | 503 |
| down | — | — | `unavailable` | 503 |

## Recovery and index rebuilding

Each worker process has its own circuit breaker, so in-process recovery only
happens in a process that saw the outage. **`reconcile_live_store`** is the
process-independent safety net. It runs at container start (Dockerfile) and
should be scheduled **every minute**:

- **Epoch missing** (Redis restarted empty, was flushed, or failed over to an
  empty replica): re-push **every** driver's availability from PostgreSQL, then
  replay pending fallback fixes.
- **Fallback rows present:** resync drivers changed since the oldest row, replay
  the rows, then delete them.
- **Otherwise:** no-op. It exits non-zero while Redis is still down.

| Situation | What happens / what to run |
|---|---|
| Redis briefly unreachable | Automatic: circuit breaker → PostgreSQL → in-process recovery plus `reconcile_live_store` |
| Redis restarted / flushed / replaced (data lost) | Automatic within a minute: `reconcile_live_store` detects the missing epoch. Live positions return with each driver's next fix (seconds); availability comes from PostgreSQL. |
| Suspected drift between Redis and PostgreSQL | `make rebuild-live` (`rebuild_live_index`): authoritative availability resync for every driver plus H3 reindex. Idempotent; safe while traffic flows. |
| `H3_RESOLUTION` changed | `make reindex-locations`: Lua-guarded cell moves in Redis (and in the fallback table) |
| Memory housekeeping | `make prune-live` every minute |
| Drivers silent too long | `make expire-stale` every minute (unchanged; reads freshness through the store) |

Verified on the Docker stack (`docker compose stop redis` / `start redis`):
- **While Redis was down:** updates were accepted (written to PostgreSQL), nearby
  search kept working, and health reported `degraded` with HTTP 200.
- **After Redis came back empty:** `reconcile_live_store` reported
  `resync_all, replayed 1`. The driver's hash came back with its availability and
  outage-time position, nearby search found the driver, and the fallback table
  was empty.
- **Driver then went offline:** the driver was removed from every Redis index.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LOCATION_STORE` | `redis` | `redis` or `postgres` |
| `LOCATION_STORE_FALLBACK` | `postgres` | `postgres` (degrade) or `none` (503) |
| `LOCATION_STORE_CIRCUIT_COOLDOWN_SECONDS` | 5 | Time to skip Redis after a failure |
| `REDIS_URL` | `redis://localhost:6380/0` | Compose sets `redis://redis:6379/0` for the backend container |
| `REDIS_KEY_PREFIX` | `rh` | Namespace (tests use a random prefix per test) |
| `REDIS_SOCKET_TIMEOUT` / `REDIS_CONNECT_TIMEOUT` | 0.5 / 0.5 s | |
| `REDIS_RETRY_ATTEMPTS` | 2 | Command retries on connection/timeout errors |
| `REDIS_HEALTH_CHECK_INTERVAL` | 30 s | Idle-connection PING |
| `LIVE_DRIVER_TTL_SECONDS` | `DRIVER_AUTO_OFFLINE_SECONDS` (900) | Key TTL for live driver data |
| `REDIS_HOST_PORT` / `REDIS_MAXMEMORY` | 6380 / 256mb | Compose only |

## Metrics

| Metric | Meaning |
|---|---|
| `location.store.failover{op}` | primary call failed, served by the fallback |
| `location.store.fallback_call{op}` | call served by the fallback while the circuit was open |
| `location.store.recovered` | in-process recovery ran |
| `location.store.reconciled{keyspace_lost}` | reconcile did work |
| `location.store.recovery_replayed` | fixes replayed in the last recovery (gauge) |
| `location.store.cas_retry` | concurrent move detected, client retried |
| `location.store.availability_seeded` | unknown status seeded from PostgreSQL |
| `location.store.filtered_stale` | index hits rejected at read time (expired, stale or moved) |

## Known limitations

- **Single Redis node.** All keys share one cluster slot (`{live}`). Scaling out
  needs sharding (for example per region) in a later step.
- **Brief degraded reads after data loss.** After Redis loses its data, nearby
  search is empty for drivers who haven't reported yet. That gap is about one
  driver ping interval.
- **Recovery runs inside a request.** In-process recovery runs synchronously in
  the first request after the outage. With many drivers, move it to a background
  worker (later step); `reconcile_live_store` already exists for that.
- **Throttling is still per process.** API throttling still uses the per-process
  local-memory cache. Moving it to Redis would make every request depend on Redis,
  so that is deferred until Redis is highly available.
