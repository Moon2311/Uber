# Location Service (Step 2)

The location service ingests driver GPS fixes, keeps exactly one *latest* location
per driver, decides whether that location is fresh enough for matching, and
optionally keeps a *sampled* history. PostgreSQL is the only storage for now; the
storage sits behind an interface so Redis can replace or front it later.

## Update flow: Driver App → Location API → storage

```mermaid
sequenceDiagram
    participant App as Driver app
    participant API as POST /api/v1/locations/update/
    participant Svc as LocationService
    participant Store as LocationStore (PostgreSQL)
    participant Hist as DriverLocationEvent (sampled)

    App->>API: {lat, lng, accuracy_m, heading, speed_kmh, recorded_at} + Bearer JWT
    API->>API: JWT → user → role DRIVER (IsDriver), throttle 120/min
    API->>API: LocationUpdateSerializer: ranges, tz-aware timestamp,<br/>not in future (>30 s), unknown fields (e.g. driver_id) → 400
    API->>Svc: record_driver_update(driver = request.user.driver_profile, ...)
    Svc->>Svc: BEGIN; SELECT driver FOR UPDATE; require ONLINE (else 409 driver_offline)
    Svc->>Svc: recorded_at = min(device time or now, now)
    Svc->>Svc: h3_cell = latlng_to_cell(lat, lng, H3_RESOLUTION) (Step 3)
    alt older than LOCATION_MAX_AGE_SECONDS
        Svc-->>API: accepted=false, outcome=too_old
    else accuracy worse than LOCATION_MAX_ACCURACY_M
        Svc-->>API: accepted=false, outcome=low_accuracy
    else
        Svc->>Store: upsert_if_newer(fix)
        Note over Store: INSERT … ON CONFLICT (driver_id) DO UPDATE<br/>WHERE stored.recorded_at < new.recorded_at
        alt newer
            Store-->>Svc: applied
            Svc->>Hist: insert only if moved ≥100 m and ≥30 s,<br/>or ≥300 s since last event
            Svc-->>API: accepted=true, outcome=applied
        else equal or older
            Store-->>Svc: not applied, current stored fix
            Svc-->>API: accepted=false, outcome=out_of_order
        end
    end
    Svc->>Svc: COMMIT; metrics + structured log
    API-->>App: 200 {accepted, outcome, freshness, lat, lng, …stored fix}
```

### Code map

| Concern | Location |
|---|---|
| HTTP endpoint, response shape | `backend/apps/locations/views.py` |
| Payload validation | `backend/apps/locations/serializers.py` |
| Rules (status, age, accuracy, ordering, history, freshness) | `backend/apps/locations/service.py` |
| Storage interface + PostgreSQL implementation | `backend/apps/locations/stores.py` |
| Latest location / history tables | `backend/apps/locations/models.py` |
| Availability = ONLINE + fresh, auto-offline | `backend/apps/drivers/services.py` |
| Candidate search (H3 + bbox fallback) | `backend/apps/drivers/candidates.py` |
| H3 cells / rings / bounds | `backend/apps/geoindex/` |
| Metrics hooks | `backend/apps/common/metrics.py` |
| JSON / key-value log formatters, redaction | `backend/apps/common/logging.py` |

## API

### `POST /api/v1/locations/update/` (driver only)

| Field | Type | Required | Rules |
|---|---|---|---|
| `lat` | number | yes | −90…90; rounded to 6 dp (~0.1 m) |
| `lng` | number | yes | −180…180; rounded to 6 dp |
| `accuracy_m` | number | no | 0…10000; fixes worse than `LOCATION_MAX_ACCURACY_M` are not applied |
| `heading` | number | no | 0 ≤ h < 360 |
| `speed_kmh` | number | no | 0…400 |
| `recorded_at` | ISO-8601 with offset | no | device fix time; defaults to server time; >30 s in the future → 400; naive → 400 |

Any other field (for example `driver_id`) is rejected with 400. The driver is
**always** the authenticated user.

Responses:

| Status | Meaning | Client action |
|---|---|---|
| 200 `accepted: true, outcome: applied` | Stored as latest | — |
| 200 `accepted: false, outcome: out_of_order` | A newer (or same-time) fix is already stored | Drop it, do **not** retry |
| 200 `accepted: false, outcome: too_old` | Fix older than `LOCATION_MAX_AGE_SECONDS` | Drop it |
| 200 `accepted: false, outcome: low_accuracy` | Accuracy too poor for matching | Keep sending |
| 400 `validation_error` | Invalid payload | Fix the client |
| 401 / 403 | Not authenticated / not a driver | Re-login |
| 409 `driver_offline` | Driver is offline | Go online first |
| 429 | Over 120 updates/min | Back off |

The response always includes the **stored** fix (`lat`, `lng`, `heading`,
`speed_kmh`, `accuracy_m`, `recorded_at`, `updated_at`) plus `freshness`
(`FRESH` / `STALE` / `MISSING`). The Step 1 response fields are unchanged, so
existing clients keep working.

Example:

```bash
curl -X POST localhost:8000/api/v1/locations/update/ \
  -H "Authorization: Bearer $DRIVER_TOKEN" -H "Content-Type: application/json" \
  -d '{"lat":37.7749,"lng":-122.4194,"accuracy_m":8,"heading":45,"speed_kmh":20,
       "recorded_at":"2026-10-28T14:05:25Z"}'
```

### `GET /api/v1/locations/me/` (driver only)

Returns `{freshness, stale_after_seconds, location}` for the driver's own stored fix.

### `GET/POST /api/v1/drivers/availability/`

Unchanged request. The response now also includes `is_available` (true when the
driver is ONLINE **and** their location is FRESH), `location_freshness` and
`last_location_at`.

## Semantics

**Out-of-order protection.** Ordering uses `recorded_at`, the device fix time,
because updates can arrive late through retries or buffering on a flaky network.
The UPSERT's `WHERE stored.recorded_at < new.recorded_at` guard runs atomically
inside PostgreSQL, so concurrent requests cannot interleave and overwrite a newer
fix. A test with 8 threads writing 40 shuffled fixes verifies this. Equal
timestamps count as duplicates and are ignored, which makes client retries
idempotent.

**Clock handling.** Device time is clamped to server time, so a fast device clock
can't extend freshness or block later fixes. Fixes more than 30 s in the future
are rejected outright.

**Staleness.**
- A fix is FRESH while `recorded_at >= now − DRIVER_LOCATION_STALE_SECONDS`
  (default 120 s).
- Nearby search and `is_available` only consider ONLINE drivers with a FRESH fix.
- `manage.py expire_stale_drivers` switches ONLINE drivers OFFLINE when they have
  been silent longer than `DRIVER_AUTO_OFFLINE_SECONDS` (default 900 s).
- A driver who changed status within that same window is in a grace period and
  is skipped.
- Drivers on an active trip are never switched off automatically; the command
  emits a warning and the `driver.stale_on_trip` metric instead.
- Run the command every minute (cron for now; Celery beat in a later step).

**Going offline.** The driver's latest location is deleted, for privacy and so a
stale position is never reused. The driver row lock means a location update that
races with going offline can't bring the location back.

**History.** `DriverLocationEvent` gets a row only when the driver has moved
≥ `LOCATION_HISTORY_MIN_DISTANCE_M` **and** ≥ `LOCATION_HISTORY_MIN_INTERVAL_SECONDS`
have passed since the last event, or when ≥ `LOCATION_HISTORY_MAX_INTERVAL_SECONDS`
have passed (a heartbeat).
- With pings every 4 s, that is at most about 2 rows/min while driving and
  12 rows/hour while parked, compared with 900 rows/hour unsampled.
- Ignored updates never create history.
- `manage.py prune_location_history` deletes rows older than
  `LOCATION_HISTORY_RETENTION_DAYS`, in batches.
- Set `LOCATION_HISTORY_ENABLED=false` to turn history off.

## Observability

Metrics are emitted through `apps.common.metrics.metrics()`. Select the backend
with `METRICS_BACKEND`: `LoggingMetrics` (the default) writes DEBUG log lines,
`NoopMetrics` does nothing, and `InMemoryMetrics` is used in tests. A
StatsD/Prometheus backend only needs to implement `increment`, `timing` and
`gauge`.

| Metric | Type | Tags |
|---|---|---|
| `location.update` | counter | `outcome` = applied / out_of_order / too_old / low_accuracy / rejected_offline |
| `location.update.duration_ms` | timing | — |
| `location.history.recorded` | counter | — |
| `driver.availability.changed` | counter | `to`, `reason` = driver_request / stale_location |
| `driver.stale_on_trip` | counter | — |
| `driver.auto_offline.last_run` | gauge | — |
| `drivers.nearby.search` / `drivers.nearby.results` | counter / gauge | — |

Logs are structured. Set `LOG_FORMAT=json` for production (one JSON object per
line) or `LOG_FORMAT=text` for local key=value output. Events:
`location.update` (`driver_id`, `outcome`), `location.cleared`,
`driver.availability.changed` (`driver_id`, `status`, `reason`) and
`driver.stale_on_trip`. **Coordinates are never logged**, and tokens and
passwords are redacted by `RedactSensitiveDataFilter`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LOCATION_STORE_BACKEND` | `apps.locations.stores.PostgresLocationStore` | Storage implementation |
| `DRIVER_LOCATION_STALE_SECONDS` | 120 | FRESH → STALE threshold |
| `DRIVER_AUTO_OFFLINE_SECONDS` | 900 | Silence before auto-offline |
| `LOCATION_MAX_AGE_SECONDS` | 300 | Older fixes are not applied |
| `LOCATION_MAX_FUTURE_SKEW_SECONDS` | 30 | Allowed device clock drift into the future |
| `LOCATION_MAX_ACCURACY_M` | 500 | Worse fixes are not applied |
| `LOCATION_HISTORY_ENABLED` | true | Sampled history on/off |
| `LOCATION_HISTORY_MIN_INTERVAL_SECONDS` | 30 | History sampling |
| `LOCATION_HISTORY_MIN_DISTANCE_M` | 100 | History sampling |
| `LOCATION_HISTORY_MAX_INTERVAL_SECONDS` | 300 | Stationary heartbeat |
| `LOCATION_HISTORY_RETENTION_DAYS` | 30 | Pruning window |
| `THROTTLE_LOCATION_UPDATE` | 120/min | Per-user rate limit |
| `METRICS_BACKEND` | `apps.common.metrics.LoggingMetrics` | Metrics sink |
| `LOG_FORMAT` | json | `json` or `text` |

## Replacing / supplementing storage with Redis (later step)

`LocationStore` (in `stores.py`) is the only thing that touches latest-location
storage. A `RedisLocationStore` must keep the same contract:

- `upsert_if_newer(fix) -> (applied, current)`: atomic compare-and-set on
  `recorded_at`, for example a Lua script that does `HGET ts` and compares before
  `HSET`.
- `get(driver_id)`, `remove(driver_id)` (idempotent).
- `find_in_bbox(bbox, fresh_after)`: Redis `GEOSEARCH` plus a timestamp check
  (used by the retained bbox fallback).
- `find_in_cells(cells, fresh_after)`: one Redis set per H3 cell (added in
  Step 3, see [h3-geo-indexing.md](h3-geo-indexing.md)).

`backend/apps/locations/tests/test_store.py` is written as a contract suite
parametrised over store classes, so adding the Redis store means adding it to
`params=[...]`.

Driver availability (ONLINE, active vehicle, category) deliberately stays in
PostgreSQL and in the drivers domain, so the store never needs to know about it.

**Known trade-off.** The driver row lock (`SELECT … FOR UPDATE`) adds a PostgreSQL
round trip per update. It is what guarantees "offline drivers never keep a
location". With Redis, availability will be cached alongside the location and the
lock replaced by a versioned status check.
