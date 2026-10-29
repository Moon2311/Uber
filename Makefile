PY := backend/.venv/bin/python

.PHONY: db up down migrate run test lint format expire-stale prune-history reindex-locations reconcile-live rebuild-live prune-live topics relay consume events-status prune-events match ws ws-cleanup reconcile-trips surge payments notify consume-fraud consume-notifications payments-status ci e2e load-gps load-ws load-rides load-mixed drills restore-drill k8s-build k8s-alerts scheduler ops-monitor schema

db:            ## start PostgreSQL + Redis + Kafka
	docker compose up -d db redis kafka
up:            ## full local stack (add --profile observability for Prometheus on :9090)
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
reconcile-live: ## replay Redis-outage fallback writes / resync after Redis data loss (every minute)
	cd backend && ../$(PY) manage.py reconcile_live_store
rebuild-live:  ## full rebuild of the Redis live index from PostgreSQL
	cd backend && ../$(PY) manage.py rebuild_live_index
prune-live:    ## drop expired drivers from Redis indexes (every minute)
	cd backend && ../$(PY) manage.py prune_live_index
topics:        ## create Kafka topics with their retention policies
	cd backend && ../$(PY) manage.py ensure_kafka_topics --wait 60
relay:         ## run the outbox relay (long-lived)
	cd backend && ../$(PY) manage.py run_outbox_relay
consume:       ## run the analytics consumer group (long-lived)
	cd backend && ../$(PY) manage.py run_event_consumer analytics
events-status: ## outbox backlog, Kafka reachability, consumer lag + heartbeats
	cd backend && ../$(PY) manage.py event_pipeline_status
prune-events:  ## delete old published outbox rows / dedupe records (daily)
	cd backend && ../$(PY) manage.py prune_event_tables
match:         ## run the matching worker (long-lived)
	cd backend && ../$(PY) manage.py run_matching_worker
ws:            ## run a WebSocket (ASGI) server on :8001
	cd backend && .venv/bin/daphne -b 0.0.0.0 -p 8001 config.asgi:application
ws-cleanup:    ## drop presence entries of dead WebSocket connections (every minute)
	cd backend && ../$(PY) manage.py cleanup_realtime_presence
reconcile-trips: ## check trips vs audit/matching/payments/outbox (add ARGS=--fix to repair)
	cd backend && ../$(PY) manage.py reconcile_trips $(ARGS)
surge:         ## recompute surge zones (long-lived; --once via ARGS)
	cd backend && ../$(PY) manage.py update_surge $(ARGS)
payments:      ## charge completed card trips + reconcile ambiguous outcomes (long-lived)
	cd backend && ../$(PY) manage.py run_payments_worker
notify:        ## deliver push notifications with retries (long-lived)
	cd backend && ../$(PY) manage.py run_notification_worker
consume-fraud: ## fraud monitoring consumer group (long-lived)
	cd backend && ../$(PY) manage.py run_event_consumer fraud
consume-notifications: ## notification consumer group (long-lived)
	cd backend && ../$(PY) manage.py run_event_consumer notifications
payments-status: ## payment states, open attempts, payments needing review (exit 1 if any)
	cd backend && ../$(PY) manage.py payments_status
scheduler:     ## periodic maintenance jobs (single active via advisory lock; long-lived)
	cd backend && ../$(PY) manage.py run_scheduler
ops-monitor:   ## print current system gauges once (freshness, backlogs, lag, DB health)
	cd backend && ../$(PY) manage.py run_ops_monitor --once
ci:            ## run the CI checks locally (lint, audit, migrations, deploy checks, tests, image, k8s)
	deploy/scripts/ci-local.sh
e2e:           ## end-to-end smoke test against the running compose stack
	cd backend && ../$(PY) loadtest/e2e_smoke.py
LOAD_USERS ?= /tmp/load-users.json
load-users:    ## seed/refresh load-test users in the compose stack (dev only)
	docker compose exec -T backend python manage.py seed_load_users --drivers 400 --riders 300 --out /tmp/load-users.json
	docker compose cp backend:/tmp/load-users.json $(LOAD_USERS)
load-gps:      ## stepped GPS-update load (needs load-users)
	cd backend && ../$(PY) loadtest/run_load.py gps --users $(LOAD_USERS) --rates 50,100,200 --step 30
load-ws:       ## WebSocket connection hold (needs load-users)
	cd backend && ../$(PY) loadtest/run_load.py ws --users $(LOAD_USERS) --connections 500,1000,2000 --hold 30
load-rides:    ## ride requests + matching + completion (needs load-users)
	cd backend && ../$(PY) loadtest/run_load.py rides --users $(LOAD_USERS) --ride-rate 5 --step 60 --drivers 200
load-mixed:    ## all of the above at once
	cd backend && ../$(PY) loadtest/run_load.py mixed --users $(LOAD_USERS) --rates 150 --connections 1000 --ride-rate 5 --step 60 --drivers 200
drills:        ## DESTRUCTIVE (local stack only): Redis/Kafka/DB/worker/WebSocket/payment failure drills
	cd backend && ../$(PY) loadtest/failure_drills.py
restore-drill: ## backup the local DB, restore into a scratch DB, verify, drop it
	set -a && . ./.env && set +a && deploy/scripts/backup_restore_drill.sh
k8s-build:     ## render the production manifests (needs docker)
	docker run --rm -v "$$PWD/deploy/k8s:/k" -w /k registry.k8s.io/kustomize/kustomize:v5.4.3 build overlays/production
k8s-alerts:    ## regenerate deploy/k8s/base/alerts.yaml from deploy/prometheus/alerts.yml
	$(PY) -c "import yaml; r=yaml.safe_load(open('deploy/prometheus/alerts.yml')); d={'apiVersion':'monitoring.coreos.com/v1','kind':'PrometheusRule','metadata':{'name':'ride-hailing'},'spec':r}; f=open('deploy/k8s/base/alerts.yaml','w'); f.write('# GENERATED from deploy/prometheus/alerts.yml (single source of truth): keep in sync\n# with \x60make k8s-alerts\x60.\n'); yaml.safe_dump(d,f,sort_keys=False,width=200)"
schema:
	cd backend && ../$(PY) manage.py spectacular --file ../docs/openapi.yml
