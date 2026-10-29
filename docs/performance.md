# Performance: measurements, bottlenecks, scaling

> **Read this first.** Every number below was measured on **one developer laptop**:
> 12 cores, 15 GB RAM, about 4 GB free. The whole docker compose stack (18
> containers, including PostgreSQL, Redis and Kafka) shared that machine with the
> load generator. The numbers show how the system *behaves* (where it saturates,
> what fixes it, what stays correct). They are **not capacity claims** for any
> production environment. Re-run `make load-*` on production-like infrastructure
> before sizing anything.

Tools: `backend/loadtest/run_load.py` (open-loop, Poisson arrivals, so a slow
server shows up as latency, not as silently reduced load) and
`backend/loadtest/e2e_smoke.py`. One generator process issues at most about
150–160 req/s, so higher rates used two processes.

## Results (2026-10-29, commit after Step 9)

### GPS updates (`POST /locations/update/`, Redis live store)

| API config | Offered | Achieved | Success | p50 | p95 | p99 |
|---|---|---|---|---|---|---|
| 3 gunicorn workers | 50/s | 49/s | 100% | 18 ms | 26 ms | 37 ms |
| 3 workers | 100/s | 92/s | 100% | 17 ms | 30 ms | 47 ms |
| 3 workers | 200/s | 164/s ¹ | 100% | 16 ms | **97 ms** | **675 ms** |
| 3 workers | 400/s | 261/s ¹ | 100% | 16 ms | **450 ms** | **881 ms** |
| 9 workers, 2 generators | 2 × 100/s | 179/s | 100% | 19 ms | 44 ms | 66 ms |
| 9 workers, 2 generators | 2 × 200/s | **304/s** | 100% | 22 ms | **44 ms** | **56 ms** |

¹ Generator-limited issue rate.

With 3 workers the API container sat at about 200% CPU while PostgreSQL stayed
below 30%, so the first bottleneck is **Python CPU in the API process**, not the
database.

### WebSocket connections (2 daphne servers, heartbeat every 20 s)

| Connections | Connected | Alive after hold | Connect p95 | Ping→pong p95 | Notes |
|---|---|---|---|---|---|
| 500 | 500 | 500 | 427 ms | 113 ms | |
| 1000 | 1000 | 1000 | 357 ms | 179 ms | |
| 2000 | **1923** | 1923 | 468 ms | 277 ms | 77 refused: **open-file limit 1024** (fixed) |
| 2000 (fd 65536) | 2000 | 2000 | 343 ms | 234 ms | |
| 4000 (fd 65536) | 4000 | 4000 | 454 ms | **788 ms** | each daphne process near 100% of one core |

Memory is about 90 MB idle and about 180 MB at roughly 2000 sockets per
process (≈ 45 KB per connection).

### Ride requests → matching → assignment → completion

Drivers are connected over WebSocket and auto-accept offers. Every trip was
driven to completion and charged.

| Load | Matching workers | Trips | Completed | Request API p95 | Request→offer p50 / p95 | Request→assigned p95 |
|---|---|---|---|---|---|---|
| 2/s, 100 drivers | 1 | 114 | 114 | 69 ms | 365 / 630 ms | 691 ms |
| 5/s, 200 drivers | 1 | 298 | 298 | 93 ms | 431 / 650 ms | 702 ms |
| 10/s, 200 drivers | 1 | 599 | 599 | 110 ms | 701 / **2262 ms** | **2337 ms** |
| 10/s, 200 drivers | **3** | 579 | 579 | 165 ms | 288 / **641 ms** | **716 ms** |

At 10/s the single matching worker sat at about 107% CPU (rounds p50 86 ms,
p95 226 ms, sequential), so jobs queued. Three workers removed the queue.

### Mixed: 150 GPS/s + 1000 sockets + 5 rides/s (9 API workers, 3 matching workers)

| Stream | Result |
|---|---|
| GPS | 100% success, p50 32 ms, p95 294 ms, p99 805 ms |
| WebSockets | 1000/1000 held, ping→pong p95 349 ms |
| Rides | 308/308 completed. Request→offer p50 409 ms, **p95 15.6 s**: see "offer TTL" below |

### Correctness after all load runs

| Invariant (1,704 load trips) | Result |
|---|---|
| Driver with two active trips | **0** |
| Trip with two accepted offers | **0** |
| Broken trip audit chain | **0** |
| Completed card trip not PAID | **0** (1,591 PAID) |
| Double charge (sandbox ledger) | **0** |
| Outbox events not published | **0** (19,846 published) |

## Bottlenecks found, and what was done

| # | Bottleneck (measured) | Fix | Effect (measured) |
|---|---|---|---|
| 1 | API CPU: 3 sync workers saturate around 160 GPS/s (p99 675 ms) | `GUNICORN_WORKERS` env; Kubernetes runs 4 per pod plus an HPA on CPU | 9 workers: 304/s at p99 56 ms |
| 2 | 2 queries per driver request: `IsDriver` lazily loads `driver_profile` | `JWTAuthenticationWithProfile` (`select_related`) | 2 → **1** query/request; −5…7% server time (7.4 → 7.05 ms in-process) |
| 3 | Open-file limit 1024 per container → about 1000 sockets per daphne | compose `ulimits.nofile 65536`; nodes must allow it | 4000/4000 sockets |
| 4 | One matching worker saturates near 10 trips/s | Run several (SKIP LOCKED + leases already make it safe); HPA on CPU | p95 request→offer 2.26 s → 0.64 s |
| 5 | Tail latency = offer TTL whenever the first driver ignores an offer | none (by design); tune `MATCHING_OFFER_TTL_SECONDS` vs real ignore rates | 29/308 trips needed a second offer → p95 ≈ 15 s |
| 6 | daphne: one core per process; about 2000 active sockets → pong p95 788 ms | Several daphne pods (HPA on CPU, slow scale-down) | not measured beyond 2 processes |

Profile of one GPS update in process (cProfile, 400 requests):

- DRF `initial()` (authentication, permissions, throttles) was **44%**, and
  authentication plus the extra profile query were most of that.
- The location write (Redis Lua plus H3) was 23%.
- Throttling (Redis) was 0.24 ms.

## Horizontal scaling (what scales how)

| Component | Scales by | Safe to replicate because | Limits to watch |
|---|---|---|---|
| API (gunicorn) | pods × workers, HPA on CPU | stateless; JWT; shared Redis throttle cache | DB connections: pods × workers × 1 (use PgBouncer beyond ~200) |
| WebSockets (daphne) | pods, HPA on CPU, slow scale-down | Redis channel layer; clients resync on reconnect | Redis channel-layer throughput; fds; scale-in disconnects clients |
| Matching workers | replicas, HPA on CPU | SKIP LOCKED job claims, fenced leases, DB unique constraints | DB lock contention on hot zones (`AssignmentConflictsHigh`) |
| Outbox relay | 1 active (+ standby) | advisory lock: strict publish order | throughput of one relay (batching; partition the outbox by key range if ever needed) |
| Kafka consumers | replicas ≤ partitions (6) | consumer groups + idempotent ledger | partitions cap parallelism: raise partitions before traffic |
| Payments / notifications workers | replicas | SKIP LOCKED + leases + idempotency keys | provider rate limits |
| PostgreSQL | vertical, then read replicas, then partitioning (see below) | | single writer: the real ceiling of this design |
| Redis | vertical / cluster (keys use `{hash tags}` for co-location) | rebuildable from PostgreSQL | memory (noeviction) |

## Geographic partitioning (beyond one metro)

Rides are local: a pickup in one city never needs a driver in another. That is
what makes partitioning tractable.

1. **Cell-based routing.**
   - The key is the H3 cell at a coarse resolution (res 3–4 ≈ a metro region).
   - Map each region to a **regional stack**: its own PostgreSQL primary, Redis,
     Kafka and workers, in the cloud region nearest the city.
   - The edge (DNS / API gateway) routes by the client's city, and the trip
     request carries its pickup cell.
2. **What stays global:**
   - accounts and authentication (small, read-mostly, replicated),
   - payment-provider customer records,
   - fraud signals that cross regions (shipped asynchronously over Kafka
     MirrorMaker).
3. **Within a region:**
   - Kafka topics are keyed by `trip_id` (ordering) and `driver_id`
     (availability).
   - Matching contention concentrates in hot zones, so matching workers can be
     sharded by zone (claim only jobs whose pickup cell hashes to the worker's
     shard) once lock conflicts show up.
4. **Cross-boundary trips** (an airport on a region edge): the pickup's region
   owns the trip. Supply search near borders queries the neighbouring region's
   live store read-only.
5. **Migration path.** Add `region` to Trip, MatchingJob and Outbox
   (default = current single region), route by it, then split one database per
   region with logical replication. No dual writes: the outbox stays per region.

None of this is implemented; the code base is single-region. Every
transactional invariant (one active trip per rider and per driver, one PENDING
offer per driver) is enforced by **one** PostgreSQL. Splitting regions keeps
those invariants regional, which is correct because drivers and riders are in
one place at a time.

## How to size a real deployment

1. Run `make load-gps`, `make load-ws` and `make load-rides` from a separate
   machine against staging (production-sized nodes, managed PostgreSQL, Redis
   and Kafka).
2. Find the knee per pod: the rate where p95 doubles.
3. Size with headroom against peak (Friday night, events, weather), not average:
   `pods = peak_rate / (0.6 × knee_per_pod)`.
4. Watch the alerts in `deploy/prometheus/alerts.yml`. PostgreSQL connections
   and CPU are the ceiling that does not scale horizontally.
