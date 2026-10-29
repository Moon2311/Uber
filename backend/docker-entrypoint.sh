#!/bin/sh
# Roles:
#   web      HTTP API (gunicorn)          asgi     WebSockets (daphne)
#   migrate  deploy checks + migrations   manage   any manage.py command
#   <other>  any manage.py command name (e.g. run_matching_worker)
# Migrations NEVER run on web start: N replicas starting together would race.
set -eu
role="${1:-web}"
[ $# -gt 0 ] && shift
export ROLE="$role"

if [ -n "${PROMETHEUS_MULTIPROC_DIR:-}" ]; then
  mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
  rm -f "$PROMETHEUS_MULTIPROC_DIR"/*.db  # stale files from a previous container run
fi

case "$role" in
  web)
    exec gunicorn -c gunicorn.conf.py config.wsgi:application "$@" ;;
  asgi)
    exec daphne -b 0.0.0.0 -p "${PORT:-8001}" --proxy-headers \
      --ping-interval 20 --ping-timeout 30 --application-close-timeout 10 \
      config.asgi:application "$@" ;;
  migrate)
    case "${APP_ENV:-development}" in
      staging|production) python manage.py check --deploy --fail-level ERROR ;;
    esac
    # Large backfills must not be cut off by the request-path statement_timeout.
    DATABASE_STATEMENT_TIMEOUT_MS="${MIGRATION_STATEMENT_TIMEOUT_MS:-0}" \
    DATABASE_LOCK_TIMEOUT_MS="${MIGRATION_LOCK_TIMEOUT_MS:-10000}" \
      exec python manage.py migrate --noinput "$@" ;;
  manage)
    exec python manage.py "$@" ;;
  *)
    exec python manage.py "$role" "$@" ;;
esac
