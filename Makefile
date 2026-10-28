PY := backend/.venv/bin/python

.PHONY: db up down migrate run test lint format expire-stale prune-history reindex-locations schema

db:            ## start PostgreSQL only
	docker compose up -d db
up:            ## start db + backend in docker
	docker compose up -d --build
down:
	docker compose down
migrate:
	cd backend && ../$(PY) manage.py migrate
run:           ## run backend on the host (needs `make db`)
	cd backend && ../$(PY) manage.py runserver 0.0.0.0:8000
test:
	cd backend && ../$(PY) -m pytest
lint:
	cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .
format:
	cd backend && .venv/bin/ruff format . && .venv/bin/ruff check --fix .
expire-stale:  ## switch silent drivers offline (schedule every minute)
	cd backend && ../$(PY) manage.py expire_stale_drivers
prune-history: ## delete location history past retention (schedule daily)
	cd backend && ../$(PY) manage.py prune_location_history
reindex-locations: ## recompute H3 cells after changing H3_RESOLUTION
	cd backend && ../$(PY) manage.py reindex_driver_locations
schema:
	cd backend && ../$(PY) manage.py spectacular --file ../docs/openapi.yml
