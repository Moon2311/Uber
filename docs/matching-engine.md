# Ride Matching Engine (Step 6)

A matching engine **in the style of** the DISCO dispatch architecture that Uber has
described publicly. It has four components: batch search over a geo index, k-ring
expansion, a driver state store, and an ETA service. This is our own design for
this codebase. It does **not** reproduce Uber's proprietary internal
implementation, algorithms or tuning.

Code: `backend/apps/matching/`.

## Components (kept separate)

| Component | File | Responsibility | Talks to |
|---|---|---|---|
| **Batch Search** | `discovery.py` (`BatchSearch`) | One live-index read for a set of H3 cells → fresh driver positions | `LocationService` → Redis cell sets (Step 4) |
| **K-Ring Search** | `discovery.py` (`KRingSearch`) | Expands ring 0, 1, 2 … around the pickup cell, bounded by `MATCHING_MAX_K` and `MATCHING_MAX_RADIUS_KM` | `GeoSearchService` (H3, Step 3) |
| **Driver State Store** | `state.py` | Eligibility: stale, offline, busy (active trip), reserved, incompatible vehicle, ineligible (no active vehicle / low rating), excluded (already offered this trip). Also reservation locks. | PostgreSQL (authoritative) + Redis locks (advisory) |
| **ETA service** ("Deep ETA" role) | `eta.py` | Pickup driving time for a shortlist | `OsrmEtaProvider` (routing service) or **labeled fallback** |
| Ranking | `ranking.py` | Configurable score → ordered list | — |
| Reservation / offers | `offers.py` | Atomic reserve, then offer, accept, decline, expire, cancel | PostgreSQL transactions + constraints |
| Orchestration | `orchestrator.py` | The workflow, job queue, leases, retries, deadlines | everything above |

No component reaches into another's storage. Discovery has no Redis or SQL
code, ranking is a pure function, and only the orchestrator knows the whole
workflow.

## Workflow

```mermaid
sequenceDiagram
  participant Rider
  participant API
  participant PG as PostgreSQL
  participant W as Matching worker
  participant R as Redis
  participant Driver
  Rider->>API: POST /trips/ (Idempotency-Key)
  API->>PG: trip REQUESTED + MatchingJob + outbox RideRequested (1 txn)
  W->>PG: claim due job (FOR UPDATE SKIP LOCKED, lease 30 s)
  W->>PG: trip REQUESTED → SEARCHING
  loop ring k = 0..max (stop one ring after target met)
    W->>R: Batch Search cells of ring k (fresh fixes)
    W->>PG: Driver State filter (online, vehicle, busy, reserved…)
  end
  W->>W: shortlist nearest N → ETA → rank
  loop best → next (≤ MATCHING_MAX_RESERVE_ATTEMPTS)
    W->>R: SET NX reservation lock (advisory)
    W->>PG: lock trip→job→driver, re-validate, INSERT PENDING offer (+ outbox RideOfferCreated)
  end
  Driver->>API: GET /matching/offers/ → POST accept
  API->>PG: lock trip→job→driver→offer; offer ACCEPTED; trip DRIVER_ASSIGNED (+ outbox RideAccepted)
  Note over W,PG: decline / expiry (15 s) / driver offline → offer closed, lock released, job woken → next candidate
  Note over W,PG: no candidates → backoff 2,4,8,15 s; deadline 120 s or 5 offers → trip CANCELLED "no_driver_available"
```

**Candidate expansion.** If the eligible set is smaller than
`MATCHING_TARGET_CANDIDATES`, the search expands ring by ring. Once the target is
reached (at ring ≥ 1), it searches **one more ring**, because a driver just
across a cell boundary can be closer than drivers already found. A test checks
that exactly three rings are read when the target is met in ring 1.

**ETA calls are bounded.** Only the `MATCHING_ETA_SHORTLIST` (8) nearest eligible
drivers by straight-line distance get an ETA, in one many-to-one routing request.

**Ranking.** Lower score is better:

    score = eta_weight · pickup_eta_s + rating_weight_seconds · (5 − rating)
            + fallback_eta_penalty_seconds (if the ETA is the fallback estimate)

Drivers whose ETA exceeds `max_pickup_eta_seconds` are dropped. Ties break by
straight-line distance, then driver id. The weights are in
`settings.MATCHING_RANKING`, and the ranking policy is pluggable.

## ETA: routing provider and labeled fallback

- **`ETA_PROVIDER=osrm` with `ETA_OSRM_URL`.** Calls the OSRM `/table` API with
  a 1.5 s timeout. If it times out, returns an error, or finds no route for a
  driver, the request uses the fallback (`matching.eta.provider_error` metric).
- **Default, `ETA_PROVIDER=fallback`.** This is **not a routing result**. It
  computes straight-line km × 1.4 ÷ 22 km/h + 60 s. Every result carries
  `eta_source="fallback_straight_line"` and `eta_is_fallback=true`, which is
  stored on the offer and shown to the driver as `eta_is_estimate`.
- **No routing service is configured in this repository.** Every ETA in the
  local demo is therefore the labeled fallback.
- As in Step 3: H3 grid distance and straight-line distance are not road
  distance or travel time.

## Correctness model

**PostgreSQL decides; Redis and Kafka only help.**

| Guarantee | Enforced by |
|---|---|
| A driver is reserved for at most one trip at a time | partial unique index `offer_one_pending_per_driver` + re-check under the driver row lock |
| A trip is offered to at most one driver at a time | partial unique index `offer_one_pending_per_trip` + re-check under the trip row lock |
| A driver is never offered the same trip twice (idempotency key of the offer) | unique `(trip, driver)` |
| A driver is never assigned to two active trips | `trip_one_active_per_driver` (Step 1) + check under lock on accept |
| Only one worker acts on a job | `FOR UPDATE SKIP LOCKED` claim + lease. **Every** state change re-checks the lease (fencing), so a stalled worker whose lease expired cannot create an offer. |
| No deadlocks | One global lock order: **Trip → MatchingJob → DriverProfile → RideOffer**. Receivers that run while holding a later lock never *wait* for an earlier one (`SKIP LOCKED`). |
| Failed reservation never leaks | the Redis lock is released in `finally` whenever no offer was created; release is compare-and-delete, so another trip's lock is never removed |

**Redis reservation locks are advisory.** `SET NX PX` per driver means 50
concurrent matchers don't all pile onto the same best driver. If Redis is down,
`try_acquire` returns "unknown" and PostgreSQL alone decides. That is slower
under contention, but still correct; the concurrency suite runs every race with
locks on **and** off.

**Kafka is not on the correctness path.** `RideOfferCreated` and `RideAccepted`
go to the transactional outbox (Step 5) for downstream consumers. Matching never
reads Kafka.

## Idempotency, timeouts and bounded retries

| Mechanism | Value / behaviour |
|---|---|
| `Idempotency-Key` on `POST /api/v1/trips/` | retry → same response, header `Idempotent-Replayed: true`; same key with a different body → 422 `idempotency_key_reused`; stored in the same transaction as the trip |
| Offer accept / decline | repeating the same response → 200 with the same offer; a conflicting response (decline after accept) → 409 |
| Offer TTL | `MATCHING_OFFER_TTL_SECONDS` = 15. The sweeper in the worker loop expires offers; accepting an expired offer → 409 and the trip is re-matched. |
| Redis reservation lock TTL | offer TTL + 5 s; normally released explicitly |
| Worker lease | `MATCHING_LEASE_SECONDS` = 30 |
| ETA timeout | `ETA_TIMEOUT_SECONDS` = 1.5, then fallback |
| Search bounds | ≤ 12 rings, ≤ 6 km, ETA shortlist 8 |
| Reservation attempts per round | `MATCHING_MAX_RESERVE_ATTEMPTS` = 3 |
| Retry backoff when no candidates | 2 s doubling, capped at 15 s |
| Give up | after `MATCHING_TIMEOUT_SECONDS` = 120 or `MATCHING_MAX_OFFERS_PER_TRIP` = 5 → trip `CANCELLED` (`no_driver_available`), payment record cancelled |

## Interaction with the rest of the system

These all happen synchronously, inside the originating transaction:
- **Trip requested:** a `MatchingJob` is created.
- **Rider cancels:** pending offers are cancelled, the job is closed, and
  reservations are released.
- **Driver goes offline:** that driver's pending offers are cancelled, and the
  affected jobs are woken so they re-match immediately.

## API

| Method | Path | Who |
|---|---|---|
| GET | `/api/v1/matching/offers/` | driver: own live (pending, unexpired) offers, with trip route and fare but no rider details |
| POST | `/api/v1/matching/offers/{id}/accept/` | owning driver (others get 404) |
| POST | `/api/v1/matching/offers/{id}/decline/` | owning driver |

Riders follow progress through `GET /api/v1/trips/{id}/` (`SEARCHING` →
`DRIVER_ASSIGNED`, plus driver details).

## Metrics and logs

| Metric | Type |
|---|---|
| `matching.candidates.raw`, `matching.candidates.eligible` | gauge per round |
| `matching.candidates.rejected{reason}` | counter |
| `matching.search.rings`, `matching.search.radius_km` | gauge per round |
| `matching.round.latency_ms{outcome}`, `matching.round{outcome}` | timing / counter |
| `matching.eta.latency_ms{provider}`, `matching.eta.estimates{source}`, `matching.eta.provider_error` | timing / counter |
| `matching.assignment.failed{reason}` | counter: `locked_elsewhere`, `driver_busy`, `driver_reserved`, `driver_unavailable`, `lease_lost`, `constraint_conflict`, `exhausted`, … |
| `matching.offer.created{eta_source}`, `accepted`, `declined`, `expired{how}`, `cancelled{reason}` | counters |
| `matching.offer.response_ms` | timing (offer → accept) |
| `matching.reservation_lock.unavailable`, `matching.exhausted`, `matching.round.error` | counters |

Each round logs one structured `matching.round` line with the fields `trip_id`,
`outcome`, `raw`, `eligible`, `rings`, `radius_km`, `rejected` (per-reason
counts), `attempts` and `latency_ms`. For example:

    matching.round trip_id=f26357c0-… outcome=offered raw=2 eligible=1 rings=12
      radius_km=1.679 rejected={'busy': 1} attempts=1 latency_ms=37.2

## Testing

`backend/apps/matching/tests/`:
- **`test_components.py`:**
  - fallback ETA labeling;
  - the OSRM client against a local HTTP stub (success, timeout and no-route →
    labeled fallback);
  - ranking rules, configurable weights and deterministic ties;
  - k-ring expansion and its caps;
  - every rejection reason;
  - minimum rating and category compatibility.
- **`test_flow.py`** (Redis live index + Redis locks + PostgreSQL):
  - request → offer → accept, including the outbox events and the required metrics;
  - idempotent accept, and offers being private to their driver;
  - decline → next candidate (and never re-offered);
  - expiry → next candidate, and accepting an expired offer;
  - k-ring expansion, the stop-one-ring-after rule, and rating-aware ranking;
  - incompatible vehicles;
  - backoff → deadline → cancelled, and max offers;
  - a failed reservation releasing its lock;
  - another trip's lock left untouched, matching with locks off, and with Redis
    locks down;
  - lease fencing;
  - driver offline → re-match, and rider cancel;
  - `Idempotency-Key` replay and reuse.
- **`test_concurrency.py`** (real threads with barriers, real transactions;
  scenarios parametrized with Redis locks **and** with none):
  - two requests competing for one driver;
  - 6 trips × 3 drivers × 3 workers;
  - concurrent workers claiming one job;
  - quadruple-tap accept;
  - accept racing expiry (5 rounds);
  - rider cancel racing driver accept;
  - DB constraints as the last line of defence.

  After every scenario the tests assert these invariants:
  - ≤ 1 pending offer per driver;
  - ≤ 1 pending offer per trip;
  - ≤ 1 active trip per driver.

**Bug found by the concurrency suite.** A *rider cancel racing a driver accept*
deadlocked: accept locked Job → Trip, while cancel (in the trips app) held Trip
and then updated Job through its signal receiver. The fix is the global lock
order above, with Trip first. The race tests then passed 8 repeated runs (56/56).

**Verified live** (`docker compose`, with the `matching-worker` container):
- A ride request with a retried `Idempotency-Key` returned the same trip.
- The worker offered the nearest driver with an ETA labeled
  `fallback_straight_line`. Accept → `DRIVER_ASSIGNED`; repeating the accept → 200.
- Two riders requesting at the same moment with one free driver → exactly one
  offer; the other trip backed off with that driver rejected as reserved.
- An offer was left unanswered → `EXPIRED/timeout` → the next waiting trip was
  offered the driver.
- A decline released the Redis reservation; repeating it → 200, and
  accept-after-decline → 409.
- Kafka received `RideOfferCreated` and `RideAccepted` through the outbox.

## Configuration

See the `MATCHING_*` and `ETA_*` settings in `backend/config/settings.py`.
Category compatibility (`MATCHING_CATEGORY_COMPATIBILITY`) is strict by default;
allow upgrades with, for example, `"ECONOMY": ["ECONOMY", "COMFORT"]`.

## Known limitations

- **One driver per trip at a time.** Offers are sequential: no parallel
  multi-offer, no batched global assignment (for example, Hungarian matching
  across many trips), no supply/demand-aware dispatch.
- **ETAs are estimates.** Without a configured routing service every ETA is the
  labeled straight-line estimate, and ETAs ignore live traffic.
- **Offers are polled.** Drivers fetch offers with `GET /matching/offers/`; push
  delivery comes with WebSockets (Channels) in a later step.
- **Rounds are synchronous inside the worker,** so throughput scales by running
  more worker replicas (safe by design).
- **Driver rating is static.** It is the stored profile value; no rating
  pipeline exists yet.
