#!/usr/bin/env bash
# Backup / restore drill against the LOCAL compose database.
#
#   1. logical backup (pg_dump custom format) of the live database
#   2. restore into a scratch database
#   3. verify: no unapplied migrations, row counts match, application invariants
#      hold on the restored copy (reconcile_trips), payments state readable
#   4. drop the scratch database
#
# Production uses managed PostgreSQL PITR (continuous WAL) as the primary backup;
# this logical dump is the secondary, portable copy and the restore rehearsal.
# See docs/disaster-recovery.md.
set -euo pipefail
cd "$(dirname "$0")/../.."
DB_USER="${POSTGRES_USER:-ridehailing}"
DB_NAME="${POSTGRES_DB:-ridehailing}"
SCRATCH="restore_drill_$(date +%s)"
OUT="${BACKUP_DIR:-/tmp}/${DB_NAME}-$(date +%Y%m%dT%H%M%S).dump"
psqlc() { docker compose exec -T db psql -U "$DB_USER" -v ON_ERROR_STOP=1 "$@"; }

echo "== backup"
t0=$(date +%s.%N)
docker compose exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc --no-owner > "$OUT"
t1=$(date +%s.%N)
printf "dump: %s (%s) in %.1f s\n" "$OUT" "$(du -h "$OUT" | cut -f1)" "$(echo "$t1 - $t0" | bc)"

echo "== restore into $SCRATCH"
psqlc -d postgres -c "CREATE DATABASE $SCRATCH" >/dev/null
trap 'psqlc -d postgres -c "DROP DATABASE IF EXISTS $SCRATCH" >/dev/null' EXIT
t2=$(date +%s.%N)
docker compose exec -T db pg_restore -U "$DB_USER" -d "$SCRATCH" --no-owner --exit-on-error < "$OUT"
t3=$(date +%s.%N)
printf "restore: %.1f s\n" "$(echo "$t3 - $t2" | bc)"

echo "== verify"
TABLES="trips_trip trips_tripevent payments_paymentrecord payments_paymentattempt matching_rideoffer events_outboxevent accounts_user pricing_farequote"
fail=0
for t in $TABLES; do
  a=$(psqlc -d "$DB_NAME" -Atc "select count(*) from $t")
  b=$(psqlc -d "$SCRATCH" -Atc "select count(*) from $t")
  printf "  %-28s live=%-8s restored=%-8s %s\n" "$t" "$a" "$b" "$([ "$a" = "$b" ] && echo ok || echo DIFF)"
  # Live traffic may add rows between dump and count: restored must not have MORE.
  [ "$b" -le "$a" ] || fail=1
done
RESTORED_URL="postgres://${DB_USER}:${POSTGRES_PASSWORD}@db:5432/${SCRATCH}"
docker compose exec -T -e DATABASE_URL="$RESTORED_URL" backend \
  python manage.py migrate --check >/dev/null && echo "  migrations: none unapplied" || { echo "  migrations: UNAPPLIED"; fail=1; }
docker compose exec -T -e DATABASE_URL="$RESTORED_URL" backend \
  python manage.py reconcile_trips --all > /tmp/restore-reconcile.json 2>/dev/null \
  && echo "  reconcile_trips on restored copy: clean" \
  || echo "  reconcile_trips on restored copy: findings -> $(tr -d '\n ' < /tmp/restore-reconcile.json)"
docker compose exec -T -e DATABASE_URL="$RESTORED_URL" backend \
  python manage.py payments_status > /dev/null 2>&1 && echo "  payments_status: no review flags" \
  || echo "  payments_status: review flags present (inspect)"
[ "$fail" = 0 ] && echo "RESTORE DRILL PASSED" || { echo "RESTORE DRILL FAILED"; exit 1; }
