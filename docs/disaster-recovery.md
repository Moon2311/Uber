# Backup, restore and disaster recovery

## What must be backed up (and what must not)

| Store | Authoritative for | Backup | If lost |
|---|---|---|---|
| **PostgreSQL** | everything that matters: users, trips + audit, payments + attempts, offers, outbox, fraud decisions, pricing versions | **PITR** (continuous WAL + daily base backup, managed service), **plus** a daily logical dump to separate storage / account | restore (below) |
| Redis | nothing (live locations, H3 sets, presence, throttle counters, channel layer) | **none**, by design | `rebuild_live_index` / `reconcile_live_store`; drivers re-send GPS within seconds; clients reconnect |
| Kafka | nothing (transport). Projections (analytics, fraud signals, notifications) are derived | broker replication (RF=3, min.insync=2) | consumers restart from committed offsets; outbox re-publishes what was never sent; projections can be rebuilt by replay within retention (7 days) |
| Payment provider | **what actually happened to money** | provider's responsibility | n/a: it is the reference we reconcile against |
| Secrets | keys, tokens | secret manager (versioned) | rotate; see deployment.md |

## Targets (proposed, to be agreed with the business)

| | Target | Basis |
|---|---|---|
| RPO (data loss) | ≤ 5 min | managed PostgreSQL PITR with WAL archived continuously |
| RTO (service back) | ≤ 1 h for a regional failure, ≤ 15 min for a zone failure | multi-AZ database failover is automatic (minutes); region rebuild from IaC + PITR |

These are **targets, not measurements**: only the local restore below has been
rehearsed. A full region-recovery drill on real infrastructure is still to do.

## Rehearsed: logical backup → restore → verify (local)

`make restore-drill` (`deploy/scripts/backup_restore_drill.sh`) does the
following:

1. Takes a `pg_dump -Fc` backup.
2. Restores it into a scratch database.
3. Verifies the copy:
   - row counts of key tables match the live database,
   - `migrate --check` finds nothing unapplied,
   - `reconcile_trips --all` runs the application's own invariant checks on the
     restored data,
   - `payments_status` passes.
4. Drops the scratch database.

Last run (2026-10-29, dev database with about 2.3 k trips and 19 k audit
events):

| | Result |
|---|---|
| Dump | 8.2 MB in 1.1 s |
| Restore | 2.2 s |
| Row counts | 8/8 tables identical |
| Migrations | none unapplied |
| Invariants on the copy | identical to live: one pre-existing stale dev trip, no new findings |

Dump and restore times grow roughly linearly with data size. Measure on a
production-sized copy before relying on them.

## Restore procedure (production)

1. **Stop writers.** Scale the API, realtime, workers and consumers to 0, or set
   the ingress to maintenance. Writes during a restore create split history.
2. **Restore PostgreSQL**:
   - *Point-in-time* (operator error, bad migration, corruption): restore to a
     NEW instance at the timestamp just before the incident. The managed
     service's PITR does this.
   - *Logical* (cross-account / cross-cloud): `pg_restore -d <new-db> --no-owner
     --exit-on-error <dump>`.
3. **Verify the restored database** with the drill's checks:
   ```
   DATABASE_URL=<restored> python manage.py migrate --check
   DATABASE_URL=<restored> python manage.py reconcile_trips --all
   DATABASE_URL=<restored> python manage.py payments_status
   ```
4. **Point the app at it.** Update `DATABASE_URL` in the secret, then run the
   migrate Job, which also runs `check --deploy`.
5. **Rebuild derived state.**
   - `rebuild_live_index` (Redis), or simply let drivers re-send GPS.
   - Consumers resume from their Kafka offsets.
   - Events newer than the restore point that Kafka already carried are
     duplicates of facts that no longer exist in the database. Projections must
     be rebuilt from PostgreSQL for that window (analytics), and fraud signals
     from that window should be treated as unreliable.
6. **Reconcile money (mandatory after a point-in-time restore).** Charges and
   refunds made between the restore point and the incident exist at the
   **provider** but not in our database.
   - For every trip completed in that window, look up the provider by
     idempotency key: `pay_<payment_id>_charge_<n>`.
   - The attempt rows are gone, so export the provider's charge list for the
     window and match it on `metadata.payment_id`.
   - Mark `needs_review` for every mismatch.
   - Never re-charge a rider until this is done: the charge may already exist.
7. **Resume traffic** gradually: realtime first, then the API, then workers.
   Watch `ApiErrorRateHigh`, `OutboxBacklog` and `PaymentsNeedReview`.

## Failure scenarios and what happens

These scenarios were rehearsed live on the local stack (`make drills`), last
run 2026-10-29: **24/24 checks passed**.

| Failure | Behaviour (verified) | Recovery |
|---|---|---|
| Redis down | GPS accepted via PostgreSQL fallback (200); trip requests still 201 (throttling fails **open**, alert `RateLimitCacheUnavailable`); readiness 200; health "degraded" | automatic; `reconcile_live_store` re-synced Redis (1 fix replayed); GPS back on Redis after the circuit cooldown |
| Kafka down | trips created and cancelled normally; events wait in the outbox (`pending` = 3) | outbox drained **1 s** after the broker was healthy |
| Consumer down | lag grows (18 events); pipeline status names the dead consumer after the heartbeat goes stale (**48–62 s**) | caught up **2 s** after restart |
| PostgreSQL down | API answers **503 + Retry-After** (not 500); readiness 503; liveness 200 (no restart storm); no worker crash-looped | API served again **0 s** after the DB was healthy, with no restarts |
| Matching worker SIGKILL | the other replica keeps dispatching (offer delivered) | lease expiry hands over jobs held by the dead worker |
| WebSocket server SIGKILL | client detects it via heartbeat; reconnecting to the other server restores state (active trip snapshot) | automatic (client reconnect) |
| Payment response lost (timeout) | payment stays PROCESSING, never FAILED, never charged twice | reconciliation settled it as PAID in **32 s**; exactly **1** charge at the provider |
| Bad deploy | see deployment.md: roll back the image; migrations are forward-only and backward-compatible | |
| Zone outage | pods spread across zones (topology spread), PDBs, multi-AZ data services | automatic |
| Region outage | **not automated**: single-region design (performance.md, geographic partitioning) | rebuild from IaC + PITR in the recovery region, following the restore procedure |

## Backup hygiene checklist

- [ ] PITR enabled with ≥ 7 days retention, and a restore tested quarterly.
- [ ] Daily logical dump copied to another account / region, encrypted, with
      30-day retention.
- [ ] Backups are encrypted, and access is separate from production
      credentials.
- [ ] `make restore-drill` (or its production equivalent) run after every major
      migration.
- [ ] The provider-reconciliation runbook (step 6) has been read by whoever is
      on call.
