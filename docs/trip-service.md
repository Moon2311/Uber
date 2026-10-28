# Trip service and database architecture (Step 8)

This step covers the trip lifecycle as explicit, enforced domain logic. It also
fixes which database owns each record and how the system behaves when part of a
change fails. It also records the Cassandra decision: **not introduced yet**.
The evaluation and the conditions for revisiting it are in
[§7](#7-cassandra-evaluation).

Code: `apps/trips/state_machine.py` (rules), `audit.py` (event log),
`services.py` (use cases), `reconciliation.py` (repair), and the receivers in
`apps/payments`, `apps/matching` and `apps/events`.

## 1. Lifecycle

```
REQUESTED ─► SEARCHING ─► DRIVER_ASSIGNED ─► DRIVER_ARRIVING ─► IN_PROGRESS ─► COMPLETED
    │            │  ▲            │   │              │   │
    │            │  └────────────┘   │              │   │      (driver releases:
    │            │  └────────────────┼──────────────┘   │       back to SEARCHING)
    ▼            ▼                   ▼                  ▼
                              CANCELLED
```

`TRANSITIONS` in `state_machine.py` is the only definition of legal changes.
Each edge lists the actors allowed to take it:

| From → To | Allowed actors | Triggered by |
|---|---|---|
| REQUESTED → SEARCHING | SYSTEM | matching worker, first round |
| SEARCHING → DRIVER_ASSIGNED | DRIVER | `accept_offer` |
| DRIVER_ASSIGNED → DRIVER_ARRIVING | DRIVER | `POST /trips/{id}/arrive/` |
| DRIVER_ARRIVING → IN_PROGRESS | DRIVER | `POST /trips/{id}/start/` |
| IN_PROGRESS → COMPLETED | DRIVER, ADMIN | `POST /trips/{id}/complete/` |
| DRIVER_ASSIGNED / DRIVER_ARRIVING → SEARCHING | DRIVER, ADMIN | `POST /trips/{id}/release/` |
| REQUESTED / SEARCHING / DRIVER_ASSIGNED / DRIVER_ARRIVING → CANCELLED | RIDER, SYSTEM, ADMIN | rider cancel, matching timeout |

`IN_PROGRESS` cannot be cancelled, and `COMPLETED` and `CANCELLED` are terminal.
Drivers cannot cancel a trip. They *release* it, and the trip goes back to
matching because the rider still wants a ride.

Some outcomes are **not** status changes. They are trip events recorded in the
same transaction (§3):

| Situation | Recorded as | Trip status effect |
|---|---|---|
| Offer rejected by the driver | `OFFER_DECLINED` (actor DRIVER) + `RideOfferClosed` | stays SEARCHING; job woken for the next round |
| Offer expired unanswered | `OFFER_EXPIRED` (SYSTEM, reason `timeout` / `expired_on_accept`) | stays SEARCHING |
| Offer withdrawn | `OFFER_CANCELLED` (reason `trip_cancelled`, `driver_offline`, `reconciled`) | none |
| Assignment failure (deadline or offer budget exhausted) | `ASSIGNMENT_FAILED` (metadata: rounds, offers made), then `SEARCHING → CANCELLED` by SYSTEM, reason `no_driver_available` | CANCELLED |
| Acceptance lost a race (trip cancelled first, driver became busy) | 409 to the driver; the transaction rolls back, so nothing is recorded | unchanged |

`transition()` refuses to run outside `transaction.atomic()`. It refuses
illegal edges, wrong actors, and fields other than `driver` and
`cancellation_reason`. It sets the timestamp for the target status the first
time the trip enters it (`search_started_at`, `driver_assigned_at`,
`driver_arrived_at`, `started_at`, `completed_at`, `cancelled_at`), and it sets
`cancelled_by`. Check constraints back these up:
`trip_cancelled_by_required`, `trip_completed_timestamps` and
`trip_driver_required_for_status`.

## 2. Concurrency and idempotency

The defences are layered, and each one is enough for its own class of mistake:

1. **Row locks.** Every use case locks the trip with `SELECT … FOR UPDATE`
   before reading its state. The global lock order stays
   Trip → MatchingJob → DriverProfile → RideOffer (see
   [matching-engine.md](matching-engine.md)).
2. **Compare-and-set.** The write is
   `UPDATE trips_trip … WHERE id=? AND status=? AND state_version=?`. A caller
   with a stale copy, or one that forgot the lock, matches zero rows and gets
   `StaleTripState` (409 `stale_trip_state`). It cannot overwrite newer state.
   `state_version` increases by exactly 1 per transition.
3. **Client-side optimistic locking.** `cancel`, `arrive`, `start`,
   `complete` and `release` accept an optional `expected_version`, so an action
   taken on an outdated screen is rejected instead of applied. The API returns
   `state_version` on every trip.
4. **Constraints as the final guard.** Partial unique indexes enforce one active
   trip per rider and per driver, and one PENDING offer per driver and per trip.
   `trip_event_one_per_version` makes the audit chain duplicate-free. A lost
   race shows up as an `IntegrityError`, which the API maps to 409 and never
   turns into a double assignment.
5. **Idempotency.**
   - Trip creation honours `Idempotency-Key`. The stored response is written in
     the same transaction, and concurrent duplicates serialise on the user row.
   - Cancel, milestones and release are naturally idempotent. Repeating a step
     that already happened returns the trip unchanged. This is checked *before*
     `expected_version`, so a retry after a lost response succeeds.
   - Kafka consumers deduplicate on `event_id`.

`tests/test_trip_concurrency.py` covers these with real threads and separate
connections released by a barrier:

- 8 simultaneous requests from one rider produce 1 trip, 1 payment, 1 job and
  1 `RideRequested`.
- 6 retries with one `Idempotency-Key` return 1 trip.
- A rider cancel racing a driver accept ends consistent either way.
- The offer-expiry sweeper racing an accept produces no deadlock and exactly one
  `OFFER_EXPIRED`.
- A double-tapped `complete` produces one completion and one `TripCompleted`.
- A driver release racing a rider cancel ends with the trip cancelled.
- 6 unlocked writers on stale copies produce exactly 1 winner.

## 3. Audit log (`trips_tripevent`)

The audit log is append-only and written in the transaction that made the
change. An audit row therefore exists **if and only if** the change committed.

- `STATUS_CHANGED` rows carry `state_version`, so the status history of a trip
  is a gap-free chain 0..N. Each row starts where the previous one ended, and
  the last row matches the trip row. Version 0 is the creation. Trips that
  existed before this migration got a backfilled version-0 row for their
  current state.
- Offer events carry `driver_id` and `offer_id`. Ranking inputs and ETAs go in
  `metadata`.
- `actor_type` / `actor_id` record who caused the event. `event_id` is the same
  UUID as the outbox event for `RideRequested` / `TripStatusChanged`, so an
  audit row can be matched to its Kafka message.
- Read it through `GET /api/v1/trips/{id}/events/`. This endpoint is staff only
  because it names every driver who was offered the trip. In Django admin the
  log is read-only.
- There is **no database FK to the trip on purpose.** An FK insert takes
  `FOR KEY SHARE` on the trip row. The offer-expiry sweeper holds offer locks
  (not the trip lock), so its audit inserts would wait on Trip and invert the
  lock order against `accept_offer`. Trips are never deleted (ORM `PROTECT`),
  and `reconcile_trips` reports orphans.

## 4. Payment status is separate

`Trip.status` and `PaymentRecord.status` are two state machines in two tables,
and neither writes the other's status.

- The trip domain never imports payments. `apps/payments/receivers.py` listens
  to trip signals inside the trip's transaction. It creates the PENDING record
  on request, and on cancellation it cancels a record that is **still
  PENDING**.
- If a payment is already AUTHORIZED, CAPTURED or FAILED, the payments domain
  decides what to do (release, refund).
- Completing a trip does not settle the payment, and a failed payment does not
  move the trip. Settlement will react to `TripCompleted` in the payments step.
  The event already says "never settle from this alone; PostgreSQL is
  authoritative".

## 5. Which database owns what

| Record | Owner (authoritative) | Copies / derived | Rebuild source |
|---|---|---|---|
| Users, driver profiles, vehicles | PostgreSQL | — | backups |
| **Trip** (status, driver, timestamps, `state_version`) | **PostgreSQL** `trips_trip` | WebSocket snapshots, Kafka `trip.events.v1` | backups |
| **Trip audit events** | **PostgreSQL** `trips_tripevent` | Kafka `trip.events.v1` (7 days) | backups |
| Payment records | PostgreSQL `payments_paymentrecord` | — | backups |
| Offers (= driver reservations), matching jobs | PostgreSQL | Redis advisory locks | PostgreSQL |
| Outbox | PostgreSQL `events_outboxevent` | Kafka (after relay) | — |
| Idempotency keys, consumer dedupe ledger | PostgreSQL | — | — |
| Live driver locations, H3 cell sets, availability mirror | PostgreSQL latest-location row (truth); Redis hot copy | Redis | `make rebuild-live` |
| Presence, channel layer | Redis (ephemeral, may be lost) | — | clients resync on reconnect |
| Analytics read models | PostgreSQL `analytics_*`, a **projection** | — | replay Kafka / backfill |
| Cassandra | **not used** | — | — |

## 6. Partial failures

Everything a trip change implies is written in **one PostgreSQL transaction**:
the trip row, the audit event, the payment record, the matching job and offers,
and the outbox rows. There is no second database in the write path, so there
are **no dual writes**. All other systems are fed *after* commit from durable
PostgreSQL state.

| Failure | What happens | Recovery |
|---|---|---|
| Crash or exception mid-transaction (any receiver: outbox, payments, matching, audit) | Full rollback. The trip is unchanged and nothing is half-written. The client gets 5xx/409 and may retry (idempotent). | none needed (`test_recovery.py`) |
| Kafka down | Trips proceed. Events wait in the outbox, and the relay retries with backoff while preserving order. | automatic; `make events-status` |
| Relay gives up on an event (`FAILED`) | Consumers miss it; trip state is unaffected. | `reconcile_trips` reports `outbox_gave_up`; requeue after fixing the cause |
| Redis down | Reservation locks report "unknown" and PostgreSQL alone decides. The live store fails over to PostgreSQL. | automatic |
| Matching worker dies holding a lease | The lease expires and another worker claims the job. Every write is fenced by the lease. | automatic |
| Driver never answers | The sweeper expires the offer (`OFFER_EXPIRED`) and wakes the job. | automatic |
| No driver before the deadline | `ASSIGNMENT_FAILED` is recorded, then CANCELLED by SYSTEM, and the payment is cancelled. | automatic |
| WebSocket push lost | UI only. Clients resync committed state on reconnect. | automatic |
| Manual SQL, restored backup, bug | Dependants may disagree with the trip. | `make reconcile-trips` (below) |

### `reconcile_trips`

Schedule this every few minutes. It runs read-only unless `--fix` is given. Each
repair **locks the trip and re-checks the condition** before writing, so it is
safe to run alongside live traffic.

| Check | `--fix` |
|---|---|
| `matchable_without_active_job`: REQUESTED/SEARCHING with no ACTIVE job | recreates or reactivates the job |
| `active_job_for_closed_trip` | closes the job (MATCHED / CANCELLED) |
| `pending_offer_for_non_searching_trip` | cancels the offer and releases the driver |
| `missing_payment_record` | creates it (CANCELLED if the trip is) |
| `payment_live_for_cancelled_trip` | cancels the PENDING payment |
| `audit_chain_mismatch`: trip row ≠ latest `STATUS_CHANGED` (something bypassed the state machine) | **report only**; history is never invented |
| `stuck_driver_assigned` / `stuck_driver_arriving` (> 1 h), `stuck_in_progress` (> 12 h) | report only, needs a human |
| `orphan_trip_events`, `outbox_gave_up` | report only |

The command exits with code 1 if anything is left unrepaired, so it can drive
alerts.

## 7. Cassandra evaluation

**Decision: do not introduce Cassandra now.** PostgreSQL stays the source of
truth for transactional trip state and for trip history. This section explains
the decision and states what would change it.

### Query patterns we actually have

| Query | Frequency | Served by |
|---|---|---|
| Current state of one trip (by id) | very high | PK lookup |
| Rider's trips, newest first, paginated | medium | index `(rider, -created_at)` |
| Driver's trips, newest first | medium | index `(driver, -created_at)` |
| Full event timeline of one trip | low (support, disputes) | index `(trip, id)` on `trips_tripevent` |
| Cross-trip analytics (per day, per city) | batch | Kafka projections, not the OLTP DB |

All of these are single-partition, key-ordered reads. That shape *suits*
Cassandra, but it also suits a B-tree index. The question is whether the
**volume** justifies running a second database.

### Volume estimate

Each trip produces about 8–12 audit rows (≈ 1 status row per transition plus
offer events) and about 400 bytes per row with indexes.

| Trips/day | Audit writes/s (avg / 5× peak) | Audit growth/year |
|---|---|---|
| 100 k | 12 / 60 | ≈ 150 GB |
| 1 M | 120 / 600 | ≈ 1.5 TB |
| 10 M | 1 200 / 6 000 | ≈ 15 TB |

Up to roughly 1 M trips/day this is ordinary PostgreSQL load, and the trip row
updates themselves are the heavier part. The audit rows are inserted in the
transaction that already exists, so they add **no extra round trip** and no
consistency problem.

### Why not now

- Writing to Cassandra **inside** the trip transaction is impossible: there is
  no shared transaction. Writing to it alongside PostgreSQL would be a dual
  write, which is exactly what this step must avoid.
- Cassandra can only ever be a **projection** fed from the outbox stream. It
  would add a cluster to operate (repairs, compaction, tombstones, capacity
  planning) and a reconciliation pipeline, and today it would solve no query
  that PostgreSQL does not already serve with a B-tree index.
- The next, cheaper steps are not exhausted. In order:
  1. monthly declarative partitioning of `trips_tripevent` (`created` range),
     so retention becomes `DROP PARTITION`;
  2. a read replica for history and support reads;
  3. archiving old partitions to object storage (Parquet) for analytics.

### When to revisit (any one of these)

- Sustained audit/history writes above ~10–20 k/s, or history above ~20 TB
  even after partitioning.
- History reads become a large share of primary/replica load even after
  partitioning and replicas.
- Multi-region active-active history reads with local latency requirements.
- Retention requirements (for example 7 years of regulatory history) that make
  PostgreSQL storage cost dominant.

### If triggered: the designed-ahead plan

PostgreSQL stays authoritative for **current trip state** in every phase.
Cassandra would only serve **history reads**.

**Schema (built around the queries above, one table per query):**

```sql
-- Q1: timeline of one trip (support / disputes). One partition per trip.
CREATE TABLE trip_events_by_trip (
    trip_id      uuid,
    event_seq    bigint,        -- trips_tripevent.id: global commit order
    event_id     uuid,
    event_type   text,
    state_version int,
    from_status  text,
    to_status    text,
    actor_type   text,
    actor_id     bigint,
    driver_id    bigint,
    offer_id     uuid,
    reason       text,
    metadata     text,          -- JSON
    occurred_at  timestamp,
    PRIMARY KEY ((trip_id), event_seq)
) WITH CLUSTERING ORDER BY (event_seq ASC);

-- Q2/Q3: a user's trips, newest first. Bucketed by month so no partition grows
-- without bound; the reader walks buckets backwards for pagination.
CREATE TABLE trips_by_rider (
    rider_id     bigint,
    month        int,           -- yyyymm
    created_at   timestamp,
    trip_id      uuid,
    status       text,
    state_version int,          -- LWW guard: only apply if newer
    ride_category text,
    estimated_fare decimal,
    currency     text,
    completed_at timestamp,
    cancelled_at timestamp,
    PRIMARY KEY ((rider_id, month), created_at, trip_id)
) WITH CLUSTERING ORDER BY (created_at DESC, trip_id ASC);

CREATE TABLE trips_by_driver ( /* same columns, keyed (driver_id, month) */ );
```

**Feeding it (no dual writes).** A Kafka consumer group `trip-history`
subscribes to `trip.events.v1`, which the outbox already publishes. It writes:

- an **idempotent upsert** keyed by `(trip_id, event_seq)` into
  `trip_events_by_trip`;
- an upsert into `trips_by_*`, applied only if `state_version` is newer, using a
  lightweight transaction (`IF state_version < ?`) or a client-side read-check.

Redelivery and reordering are harmless. Because a released trip changes
`driver_id`, `trips_by_driver` gets a row for every driver that was assigned.
That is the correct history.

`event_seq` is not in the Kafka payload today. It would be added as an optional
field (`TripStatusChanged` v2) or looked up from `event_id`.

**Migration plan (phased, each phase reversible):**

1. **Shadow.** Deploy the cluster and the projector. Backfill from PostgreSQL
   `trips_tripevent` / `trips_trip`, not from Kafka: Kafka keeps only 7 days.
   The backfill is idempotent upserts in id order, followed by live
   consumption from an offset recorded before the backfill started. The overlap
   is harmless because the writes are idempotent.
2. **Verify.** Nightly reconciliation (below) must be clean for 2 weeks.
3. **Dark reads.** History endpoints read both stores and compare, while still
   serving PostgreSQL.
4. **Cut over reads** for history endpoints behind a flag. The fallback is
   PostgreSQL.
5. **Shrink PostgreSQL.** Only after a retention decision, drop old
   `trips_tripevent` partitions. The most recent N months stay in PostgreSQL for
   transactional checks and reconciliation.

**Recovery.**

- Projector down: lag grows and history reads go stale. Current state is
  unaffected because it comes from PostgreSQL. Above a lag threshold, fall back
  to PostgreSQL reads.
- Data loss or corruption: truncate and re-run the backfill from PostgreSQL,
  then resume from Kafka.
- Lost events (relay `FAILED`): the reconciler fills them in from PostgreSQL.

**Cross-database reconciliation.**

- Per day, compare the count of `trips_tripevent` rows with the count of
  Cassandra rows, bucketed by `trip_id` hash.
- For mismatching buckets, compare per-trip `(max(event_seq), count,
  hash(event_ids))` and re-project the trips that differ from PostgreSQL.
- PostgreSQL always wins.

## 8. Deploying this step

1. `migrate`. It adds the columns and the audit table, and it backfills
   `cancelled_by`, `started_at` for completed trips and version-0 audit rows
   before it adds the constraints.
2. **Deploy Kafka consumers before producers.** `RideOfferClosed` is a new
   event type, and an *old* consumer cannot decode it and would dead-letter it.
   New consumers ignore types they have no handler for.
3. WebSocket servers before or together with the API, so the `trip.leave`
   control message is understood.
4. Schedule `make reconcile-trips` (read-only) and alert on a non-zero exit.
   Use `--fix` once the findings look as expected.
