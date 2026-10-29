#!/usr/bin/env bash
# Run the CI pipeline's checks locally (GitHub Actions cannot run here). Uses the
# local compose PostgreSQL / Redis / Kafka for the test suite and a scratch DB for
# the fresh-migration check. Exits non-zero on the first failure.
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=backend/.venv/bin/python
step() { printf '\n== %s\n' "$*"; }

step "lint"
(cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .)

step "dependency audit (exact packages inside the production image, Python 3.12)"
# The local venv may be an older Python that cannot install every fix; what ships
# is the image, so audit its frozen package set.
docker build -q -t ride-hailing-backend:ci backend >/dev/null
docker run --rm --entrypoint pip ride-hailing-backend:ci freeze > /tmp/rh-image-freeze.txt
docker run --rm -v /tmp/rh-image-freeze.txt:/req.txt:ro python:3.12-slim sh -c \
  "pip install -q --disable-pip-version-check pip-audit >/dev/null 2>&1 && pip-audit --progress-spinner off \
   --disable-pip --no-deps -r /req.txt \
   --ignore-vuln CVE-2026-106454 --ignore-vuln CVE-2026-115778 --ignore-vuln CVE-2026-115774 \
   --ignore-vuln CVE-2026-115775 --ignore-vuln CVE-2026-115776 --ignore-vuln CVE-2026-83813 \
   --ignore-vuln CVE-2026-115877"
(cd backend && .venv/bin/pip-audit -r requirements.txt --progress-spinner off >/dev/null 2>&1) \
  || echo "  NOTE: the local venv (python $(backend/.venv/bin/python -c 'import sys;print(sys.version.split()[0])')) has unfixable advisories; use Python 3.12 locally"

step "migrations: models agree, fresh database migrates, nothing unapplied"
(cd backend && ../$PY manage.py makemigrations --check --dry-run)
set -a; . ./.env; set +a
SCRATCH="ci_fresh_$(date +%s)"
docker compose exec -T db psql -U "$POSTGRES_USER" -d postgres -qc "CREATE DATABASE $SCRATCH"
trap 'docker compose exec -T db psql -U "$POSTGRES_USER" -d postgres -qc "DROP DATABASE IF EXISTS $SCRATCH"' EXIT
FRESH="postgres://$POSTGRES_USER:$POSTGRES_PASSWORD@localhost:${POSTGRES_HOST_PORT:-5433}/$SCRATCH"
(cd backend && DATABASE_URL=$FRESH ../$PY manage.py migrate --noinput -v0 && DATABASE_URL=$FRESH ../$PY manage.py migrate --check)

step "deployment checks (staging-like)"
(cd backend && env -i PATH="$PATH" HOME="$HOME" APP_ENV=staging DJANGO_DEBUG=false \
  DJANGO_SECRET_KEY=ci-staging-like-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz \
  JWT_SIGNING_KEY=ci-staging-like-jwt-key-0123456789-abcdefghijklmnopqrstuvwxyz \
  DJANGO_ALLOWED_HOSTS=api.staging.example.com CORS_ALLOWED_ORIGINS=https://app.staging.example.com \
  CSRF_TRUSTED_ORIGINS=https://app.staging.example.com NUM_PROXIES=1 METRICS_TOKEN=ci-metrics-token-0123456789abcdef \
  PAYMENTS_WEBHOOK_SECRETS=whsec_ci_0123456789abcdef0123456789abcdef PAYMENTS_SANDBOX_ALLOWED=true \
  ADMIN_URL_PATH=ops-ci/ DATABASE_URL="$FRESH" ../$PY manage.py check --deploy --fail-level ERROR)

step "test suite"
(cd backend && ../$PY -m pytest -q -p no:cacheprovider)

step "image non-root read-only smoke"
docker rm -f rh-ci-smoke >/dev/null 2>&1 || true
docker run -d --name rh-ci-smoke --read-only --tmpfs /tmp -e DJANGO_SECRET_KEY=x \
  -e DATABASE_URL=postgres://x@127.0.0.1:1/x -e DJANGO_ALLOWED_HOSTS=127.0.0.1 ride-hailing-backend:ci web >/dev/null
for i in $(seq 1 30); do docker exec rh-ci-smoke python /app/healthcheck.py && break; sleep 1; done
docker exec rh-ci-smoke python /app/healthcheck.py
[ "$(docker exec rh-ci-smoke id -u)" = "10001" ]
docker rm -f rh-ci-smoke >/dev/null

step "prometheus config + alert rules"
docker run --rm -v "$PWD/deploy/prometheus:/cfg" --entrypoint promtool prom/prometheus:v2.55.1 check config /cfg/prometheus.yml

step "kubernetes manifests"
for env in staging production; do
  docker run --rm -v "$PWD/deploy/k8s:/k" -w /k registry.k8s.io/kustomize/kustomize:v5.4.3 build overlays/$env > /tmp/rh-$env.yaml
  docker run --rm -v /tmp:/m ghcr.io/yannh/kubeconform:v0.6.7 -strict -summary -kubernetes-version 1.30.0 \
    -schema-location default \
    -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
    /m/rh-$env.yaml
done

step "compose"
docker compose config -q
echo; echo "LOCAL CI PASSED"
