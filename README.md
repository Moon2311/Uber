# Ride-Hailing Platform

A ride-hailing platform (rider and driver apps) built incrementally. This repository currently contains the
**backend** (Django + DRF + PostgreSQL). The React frontend from Step 1 has **not
been built yet** (see *Status*).

## Status

| Step | Scope | State |
|---|---|---|
| 1 | Accounts, roles, drivers/vehicles, trips, pricing, payment records, JWT, health | Backend done & tested; **frontend pending** |
| 2 | Location service (ordering, staleness, sampled history, store interface, metrics, logging) | Done & tested — see [docs/location-service.md](docs/location-service.md) |
| 3 | H3 indexing: cell per latest location, bounded ring expansion, bbox fallback retained | Done & tested — see [docs/h3-geo-indexing.md](docs/h3-geo-indexing.md) |
| 4 | Redis hot store: live locations, availability mirror, H3 cell sets, Lua atomicity, TTLs, failover to PostgreSQL, reconcile/rebuild | Done & tested — see [docs/redis-hot-store.md](docs/redis-hot-store.md) |
| 5 | Kafka event streaming: versioned schemas, transactional outbox + relay, idempotent consumers, retries + DLQ, lag/health monitoring, sampled GPS stream | Done & tested — see [docs/event-streaming.md](docs/event-streaming.md) |
| 6 | DISCO-style matching: batch + k-ring H3 search, driver state store, ETA provider (labeled fallback), ranking, atomic reservations/offers, leases, idempotency, concurrency-tested | Done & tested — see [docs/matching-engine.md](docs/matching-engine.md) |
| 7 | Real-time WebSockets (Channels + Daphne + Redis channel layer): authenticated sockets, authorized trip channels, offers + backend-validated accept/decline, live location & milestones, heartbeats/reconnect/stale cleanup | Done & tested — see [docs/realtime.md](docs/realtime.md) |
| 8 | Trip service & database architecture: centralized actor-aware state machine, compare-and-set `state_version`, append-only audit log, driver release/re-search, payment status decoupled, data-ownership + partial-failure model, `reconcile_trips`, Cassandra evaluated (not introduced) | Done & tested — see [docs/trip-service.md](docs/trip-service.md) |
| 9 | Pricing (versioned rules, upfront quotes, illustrative surge), payments (provider abstraction + sandbox, idempotent attempts, signed webhooks, reconciliation of ambiguous outcomes), fraud (Kafka monitoring vs blocking checks), push notifications (retries, delivery tracking) | Done & tested — see [docs/pricing-payments.md](docs/pricing-payments.md) |
| 10 | Production preparation: security review + fixes, correlation ids, Prometheus metrics + 24 alerts, Sentry, DB timeouts + graceful degradation, scheduler, hardened image, Kubernetes manifests, CI, load tests, failure drills, backup/restore drill | Done & measured locally; **not production-ready**: see [docs/production-readiness.md](docs/production-readiness.md) |

## Quick start

```bash
cp .env.example .env            # then set POSTGRES_PASSWORD / DJANGO_SECRET_KEY / DATABASE_URL
docker compose up -d --build    # migrations + Kafka topics run as one-shot jobs first, then
                                # API :8000, WebSockets :8001/:8002, all workers (127.0.0.1 only)
docker compose --profile observability up -d   # + Prometheus on :9090
curl localhost:8000/api/v1/health/ready/
make e2e                        # one full ride through every component
```

Running the backend on the host instead (for development; use Python 3.12 like the image):

```bash
python3.12 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements-dev.txt
make db          # PostgreSQL + Redis + Kafka
make topics      # create Kafka topics
make migrate
make run         # http://localhost:8000
```

API docs (development only): Swagger UI at http://localhost:8000/api/docs/; the OpenAPI schema is at `/api/schema/`.

Production: Kubernetes + managed PostgreSQL / Redis / Kafka, see
[docs/deployment.md](docs/deployment.md). Operations and alerts are in [docs/operations.md](docs/operations.md),
backup and DR in [docs/disaster-recovery.md](docs/disaster-recovery.md), and measured performance in
[docs/performance.md](docs/performance.md).

## Commands

| Command | What it does |
|---|---|
| `make ci` | Run the CI checks locally (lint, dependency audit of the image, migrations on a fresh DB, `check --deploy`, tests, image smoke, alert rules, Kubernetes manifests) |
| `make e2e` | End-to-end smoke test against the running stack |
| `make load-users` then `make load-gps` / `load-ws` / `load-rides` / `load-mixed` | Load tests (open-loop) against the local stack |
| `make drills` | **Destructive, local only**: Redis / Kafka / PostgreSQL / worker / WebSocket / payment-timeout failure drills |
| `make restore-drill` | Backup, restore into a scratch DB, verify, drop |
| `make scheduler` / `make ops-monitor` | Periodic maintenance jobs / print system gauges |
| `make test` | pytest against PostgreSQL + Redis + Kafka (throw-away test DB; Redis DB 15; per-test topic prefix, deleted afterwards) |
| `make lint` / `make format` | ruff lint + format check / autofix |
| `make expire-stale` | Switch silent drivers offline (schedule every minute) |
| `make prune-history` | Delete location history older than retention (schedule daily) |
| `make reindex-locations` | Recompute H3 cells after changing `H3_RESOLUTION` |
| `make reconcile-live` | Replay Redis-outage fallback writes / resync after Redis data loss (schedule every minute) |
| `make prune-live` | Drop expired drivers from Redis indexes (schedule every minute) |
| `make rebuild-live` | Full rebuild of the Redis live index from PostgreSQL |
| `make relay` / `make consume` | Run the outbox relay / analytics consumer on the host |
| `make match` | Run the matching worker on the host |
| `make ws` | Run a WebSocket (ASGI) server on the host at :8001 |
| `make reconcile-trips` | Check trips against audit log / matching / payments / outbox (`--fix` repairs safe cases; exit 1 if anything is left) |
| `make surge` / `make payments` / `make notify` | Run the surge updater / payments worker / notification worker on the host |
| `make consume-fraud` / `make consume-notifications` | Run the fraud / notification consumer groups |
| `make payments-status` | Payment states, open (ambiguous) attempts, payments needing review (exit 1 if any) |
| `make events-status` | Outbox backlog, Kafka reachability, consumer lag and heartbeats |
| `make prune-events` | Delete old published outbox rows and dedupe records (schedule daily) |
| `make schema` | Export OpenAPI to `docs/openapi.yml` |

## API (v1)

| Method | Path | Who |
|---|---|---|
| POST | `/api/v1/auth/register/` | anyone (rate-limited) |
| POST | `/api/v1/auth/login/` · `/auth/refresh/` | anyone (rate-limited per IP and per account; refresh tokens rotate) |
| POST | `/api/v1/auth/logout/` | authenticated (revokes the refresh token) |
| GET | `/api/v1/auth/me/` | authenticated |
| GET/POST | `/api/v1/drivers/availability/` | driver |
| GET | `/api/v1/drivers/nearby/?lat=&lng=&radius_km=&category=` | rider |
| POST | `/api/v1/locations/update/` | driver |
| GET | `/api/v1/locations/me/` | driver |
| POST | `/api/v1/pricing/estimate/` | rider: returns an upfront quote (`quote_id`, breakdown, surge, pricing version) |
| POST | `/api/v1/trips/` | rider (optional `Idempotency-Key` header, `quote_id`, `payment_method_id`) |
| GET | `/api/v1/trips/{id}/` · `/api/v1/trips/history/` | owner (rider or assigned driver) |
| POST | `/api/v1/trips/{id}/cancel/` | owning rider (idempotent; optional `expected_version`) |
| GET | `/api/v1/matching/offers/` | driver (own pending offers) |
| POST | `/api/v1/matching/offers/{id}/accept/` · `/decline/` | owning driver (idempotent) |
| POST | `/api/v1/trips/{id}/arrive/` · `/start/` · `/complete/` | assigned driver (idempotent; optional `expected_version`) |
| POST | `/api/v1/trips/{id}/release/` | assigned driver, before pickup: trip returns to matching |
| GET | `/api/v1/trips/{id}/events/` | staff (full audit trail) |
| WS | `/ws/v1/` (subprotocols `["jwt", <access token>]`) | riders & drivers — see [docs/realtime.md](docs/realtime.md) |
| GET/POST | `/api/v1/payments/methods/` · DELETE `/methods/{id}/` | rider (provider tokens only, never card numbers) |
| GET | `/api/v1/payments/trips/{trip_id}/` | owning rider / staff |
| POST | `/api/v1/payments/{id}/retry/` · `/refund/` | owning rider (failed payment) · staff |
| POST | `/api/v1/payments/webhooks/{provider}/` | payment provider (HMAC-signed) |
| GET | `/api/v1/notifications/` · POST `/notifications/devices/` | authenticated |
| GET | `/api/v1/health/live/` · `/health/ready/` | anyone (probes; minimal bodies) |
| GET | `/api/v1/health/` | anyone (status only); staff see dependency details |
| GET | `/metrics` | Prometheus (`Authorization: Bearer $METRICS_TOKEN`; not routed publicly) |
| GET | `/api/v1/health/events/` | staff (consumer lag per partition) |

Errors always use this shape: `{"error": {"code", "message", "details"}}`.

## Architecture decisions

- **Modular Django apps.** The apps are `accounts`, `drivers`, `locations`,
  `trips`, `pricing`, `payments` and `common`. Business rules live in each app's
  `services.py`/`service.py`, and views stay thin.
- **Role comes from the DB, never the request.** The JWT carries only `user_id`,
  and the role is read from the user row. Server-controlled fields
  (`driver`, `status`, `driver_id`, …) are rejected with 400 if a client sends them.
- **Ownership via queryset scoping.** Trips belonging to someone else return
  404, not 403, so their existence isn't revealed.
- **PostgreSQL as source of truth.** Check constraints cover coordinates, fares
  and statuses. Partial unique indexes enforce one active trip per rider and per
  driver, and one active vehicle per driver. Money uses `Decimal`, and all
  timestamps are timezone-aware.
- **Explicit trip state machine** in `trips/state_machine.py`. One table lists
  every legal edge and the actors allowed to take it. Writes are compare-and-set
  on `state_version`, and every change appends to an audit log in the same
  transaction. Payment status is a separate state machine that follows trip
  signals (see [docs/trip-service.md](docs/trip-service.md)).
- **Location storage behind `LocationStore`**, so Redis can be added without
  changing callers. See [docs/location-service.md](docs/location-service.md).
- **H3 is isolated in `apps/geoindex`.** Candidate search depends on cells and
  grid distances, never on a resolution, and falls back to bbox search on H3
  errors. H3 grid distance is **not** road distance or travel time (see
  [docs/h3-geo-indexing.md](docs/h3-geo-indexing.md)).
- **Redis is a disposable hot store.** It holds live locations, the availability
  mirror and H3 cell sets. PostgreSQL is the source of truth for every
  application record. Everything in Redis can be rebuilt, and the app degrades to
  PostgreSQL when Redis is down (see [docs/redis-hot-store.md](docs/redis-hot-store.md)).
- **Kafka distributes facts; it is not a source of truth.** Business code sends
  Django signals. Trip and availability events go through a transactional
  outbox (same PostgreSQL transaction), and a single relay publishes them in
  order. Consumers are idempotent through a processed-event ledger and
  dead-letter permanent failures. GPS is sampled (cell change or 15 s) to a
  1-hour topic. See [docs/event-streaming.md](docs/event-streaming.md).
- **Matching keeps its components separate**: discovery (H3/Redis), driver
  state, ETA, ranking, reservation and orchestration. PostgreSQL row locks
  (fixed order Trip → Job → Driver → Offer) and unique constraints guarantee
  that no driver gets conflicting trips. Redis locks are an advisory fast path
  and Kafka is not involved (see [docs/matching-engine.md](docs/matching-engine.md)).
- **WebSockets are a delivery layer, not a source of truth.** Pushes are sent
  after COMMIT and carry state re-read from PostgreSQL. A WebSocket accept runs
  the same transactional code as HTTP. WebSockets (UI, ephemeral) and Kafka
  (services, durable) are independent paths (see [docs/realtime.md](docs/realtime.md)).
- **Money is reconciled, not assumed.** Every charge or refund is an attempt with
  an idempotency key, committed before the provider call. Timeouts stay
  ambiguous until reconciliation asks the provider. Webhooks are HMAC-verified
  on the raw body and deduplicated. Surge is an **illustrative policy** that
  must be checked against local law. Fraud monitoring (Kafka) never blocks by
  itself; blocking checks need corroborated reliable signals, so GPS alone
  never blocks (see [docs/pricing-payments.md](docs/pricing-payments.md)).
- **Production posture is enforced, not just documented.** `APP_ENV=staging|production`
  refuses DEBUG at startup. The migrate job runs `check --deploy` with 20 app-specific
  checks: weak or shared keys, per-process rate limits, non-https origins, sandbox
  payments in production, and more. Every log line, outbox event and Kafka message
  carries the request's correlation id.
- **Cassandra: evaluated, not introduced.** At current and projected volumes,
  PostgreSQL (with partitioning and replicas next) serves every trip-history
  query. Cassandra could only be an outbox-fed projection, so it would add
  operations work without serving any new query. The trigger thresholds, a
  query-first CQL schema, the migration plan and reconciliation are in
  [docs/trip-service.md §7](docs/trip-service.md#7-cassandra-evaluation).
