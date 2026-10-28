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

## Quick start

```bash
cp .env.example .env            # then set POSTGRES_PASSWORD / DJANGO_SECRET_KEY / DATABASE_URL
docker compose up -d --build    # PostgreSQL :5433, Redis :6380, Kafka :9094, API :8000,
                                # + outbox-relay, consumer-analytics, matching-worker,
                                # WebSockets on :8001 and :8002 (ws://localhost:8001/ws/v1/)
curl localhost:8000/api/v1/health/
```

Running the backend on the host instead (for development):

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements-dev.txt
make db          # PostgreSQL + Redis + Kafka
make topics      # create Kafka topics
make migrate
make run         # http://localhost:8000
```

API docs: Swagger UI at http://localhost:8000/api/docs/; the OpenAPI schema is at `/api/schema/`.

## Commands

| Command | What it does |
|---|---|
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
| `make events-status` | Outbox backlog, Kafka reachability, consumer lag and heartbeats |
| `make prune-events` | Delete old published outbox rows and dedupe records (schedule daily) |
| `make schema` | Export OpenAPI to `docs/openapi.yml` |

## API (v1)

| Method | Path | Who |
|---|---|---|
| POST | `/api/v1/auth/register/` | anyone (rate-limited) |
| POST | `/api/v1/auth/login/` · `/auth/refresh/` | anyone (rate-limited) |
| GET | `/api/v1/auth/me/` | authenticated |
| GET/POST | `/api/v1/drivers/availability/` | driver |
| GET | `/api/v1/drivers/nearby/?lat=&lng=&radius_km=&category=` | rider |
| POST | `/api/v1/locations/update/` | driver |
| GET | `/api/v1/locations/me/` | driver |
| POST | `/api/v1/pricing/estimate/` | rider |
| POST | `/api/v1/trips/` | rider (optional `Idempotency-Key` header) |
| GET | `/api/v1/trips/{id}/` · `/api/v1/trips/history/` | owner (rider or assigned driver) |
| POST | `/api/v1/trips/{id}/cancel/` | owning rider |
| GET | `/api/v1/matching/offers/` | driver (own pending offers) |
| POST | `/api/v1/matching/offers/{id}/accept/` · `/decline/` | owning driver (idempotent) |
| POST | `/api/v1/trips/{id}/arrive/` · `/start/` · `/complete/` | assigned driver (idempotent) |
| WS | `/ws/v1/` (subprotocols `["jwt", <access token>]`) | riders & drivers — see [docs/realtime.md](docs/realtime.md) |
| GET | `/api/v1/health/` | anyone |
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
- **Explicit trip state machine** in `trips/state_machine.py`.
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
- **Not yet introduced, on purpose:** Cassandra.
