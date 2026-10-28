# Ride-Hailing Platform

A ride-hailing platform (rider and driver apps) built incrementally. This repository currently contains the
**backend** (Django + DRF + PostgreSQL). The React frontend from Step 1 has **not
been built yet** (see *Status*).

## Status

| Step | Scope | State |
|---|---|---|
| 1 | Accounts, roles, drivers/vehicles, trips, pricing, payment records, JWT, health | Backend done & tested; **frontend pending** |
| 2 | Location service (ordering, staleness, sampled history, store interface, metrics, logging) | Done & tested — see [docs/location-service.md](docs/location-service.md) |

## Quick start

```bash
cp .env.example .env            # then set POSTGRES_PASSWORD / DJANGO_SECRET_KEY / DATABASE_URL
docker compose up -d --build    # PostgreSQL on host :5433, API on :8000 (migrates on start)
curl localhost:8000/api/v1/health/
```

Running the backend on the host instead (for development):

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements-dev.txt
make db          # PostgreSQL only
make migrate
make run         # http://localhost:8000
```

API docs: Swagger UI at http://localhost:8000/api/docs/; the OpenAPI schema is at `/api/schema/`.

## Commands

| Command | What it does |
|---|---|
| `make test` | pytest against PostgreSQL (creates a throw-away test DB) |
| `make lint` / `make format` | ruff lint + format check / autofix |
| `make expire-stale` | Switch silent drivers offline (schedule every minute) |
| `make prune-history` | Delete location history older than retention (schedule daily) |
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
| POST | `/api/v1/trips/` | rider |
| GET | `/api/v1/trips/{id}/` · `/api/v1/trips/history/` | owner (rider or assigned driver) |
| POST | `/api/v1/trips/{id}/cancel/` | owning rider |
| GET | `/api/v1/health/` | anyone |

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
- **Not yet introduced, on purpose:** Redis, Channels, Kafka, Cassandra, H3.
