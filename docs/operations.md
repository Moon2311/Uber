# Operations: observability, alerts, runbooks

## Logs

- **Format.** One JSON object per line (`LOG_FORMAT=json`, the default when
  deployed). `extra={...}` fields become top-level keys.
- **Redaction.** Passwords, tokens, JWTs, `Authorization` values and card-like
  fields are redacted before output (`apps/common/logging.py`).
- **Correlation.** Every line carries `request_id`:
  - It comes from the caller's `X-Request-ID` (validated `[A-Za-z0-9._-]{8,64}`)
    or is generated, and is returned in the response header.
  - It is stored on outbox rows (`correlation_id`), sent as a Kafka header, and
    re-bound by consumers while they handle the event.
  - One id therefore links: API request → outbox → Kafka → consumer logs.
  - Worker loops bind a fresh id per iteration, so a matching round's logs and
    the events it emits share one id.
  - Verified end to end: the smoke test's `smoke-req-…` id was on its
    `RideRequested` outbox row.
- **Slow or failed requests.** Requests over `SLOW_REQUEST_MS` (1000 ms) and
  every 5xx are logged as `http.slow_or_failed`, with route template, status, DB
  query count and DB time.

## Error tracking

Set `SENTRY_DSN`; any Sentry-protocol service works, including GlitchTip
(self-hosted).

- **No PII.** `send_default_pii=False`. Request bodies and cookies are dropped,
  and `Authorization`, `Cookie` and `Payment-Signature` headers are replaced.
- **Redaction.** Messages pass the log redaction, and `request_id` is attached as
  a tag.
- **Release tracking.** `APP_RELEASE` (the image sha) is reported as the
  release, so a regression points at a deploy.
- **Tracing.** Off by default (`SENTRY_TRACES_SAMPLE_RATE=0`).

## Metrics

Prometheus, pull model:

- The API serves `/metrics` (gunicorn multiprocess mode); the WebSocket servers
  serve `/metrics` too.
- Every worker serves its own on `METRICS_PORT` (9100).
- All scrapes need `Authorization: Bearer $METRICS_TOKEN`.
- Names are `rh_<dotted name with _>`. Counters end in `_total`; timings are
  histograms in milliseconds.
- Unbounded labels (H3 zones) are dropped, and labels use route *templates*,
  never ids.

State gauges (backlogs, freshness, lag, DB health) are collected every 15 s by
`run_ops_monitor`. Periodic jobs run in `run_scheduler`: one active instance,
with `rh_scheduler_last_success_ts{job}` per job.

### What is monitored, and how

| Requirement | Metric(s) | Alert |
|---|---|---|
| API latency | `rh_http_request_latency_ms{method,route,status}` (histogram); `rh_http_request_db_ms`, `rh_http_request_db_queries_total` per route | `ApiLatencyP95High` (p95 > 500 ms, 10 m), `ApiErrorRateHigh` (5xx > 2%, 5 m) |
| Active WebSocket connections | `rh_realtime_connections_inflight{role}` (summed across processes); `rh_realtime_connected_total`, `..._disconnected_total{code}`, `..._rate_limited_total`, `..._push_failed_total` | `WebSocketConnectionsDropped` (halved in 15 m), `WebSocketPushFailures` |
| Location freshness | `rh_drivers_online`, `rh_drivers_fresh_location`, `rh_drivers_online_without_fresh_location`, `rh_location_age_p95_s`; `rh_location_fix_age_ms` (device→server delay at ingestion) | `DriverLocationsStale` (> 20% of online drivers stale), `GpsIngestionDelayed` |
| Matching latency | `rh_matching_round_latency_ms{outcome}`; `rh_matching_oldest_job_age_s`; `rh_matching_jobs_active`; `rh_matching_offers_pending` | `MatchingLatencyHigh` (p95 > 2 s), `MatchingBacklog` (a trip waited > 3 min) |
| ETA latency | `rh_matching_eta_latency_ms{provider}`; `rh_matching_eta_provider_error_total` | `EtaLatencyHigh` (p95 > 1 s) |
| Assignment conflicts | `rh_matching_assignment_failed_total{reason}` (`constraint_conflict`, `locked_elsewhere`, `driver_reserved`, …); `rh_trip_transition_stale_total` | `AssignmentConflictsHigh` |
| Kafka lag | `rh_events_consumer_lag{group}`; `rh_events_outbox_pending`, `..._oldest_pending_age_s`, `..._failed` | `ConsumerLagHigh` (> 1000, 10 m), `OutboxBacklog` (> 2 min), `OutboxEventsGaveUp` |
| Database performance | `rh_db_connections`, `..._active`, `..._waiting_on_lock`, `rh_db_max_connections`, `rh_db_longest_transaction_s`, `rh_db_deadlocks_total`, `rh_db_cache_hit_ratio`, `rh_db_size_bytes`; per-route DB time | `DatabaseConnectionsNearLimit` (> 80%), `LongRunningTransaction` (> 60 s), `DatabaseLockWaits`, `DatabaseDeadlocks` |
| Payment failures | `rh_payments_transition_total{from,to}`; `rh_payments_failed_last_hour`, `..._paid_last_hour`, `..._needs_review`, `..._open_attempts`; `rh_payments_provider_timeout_total` | `PaymentFailureRatioHigh` (> 15%), `PaymentsNeedReview` (> 0, pages), `PaymentAttemptsStuck` |
| Jobs and pipeline health | `rh_scheduler_last_success_ts{job}`, `rh_ops_probe_failed_total{probe}`, `rh_cache_unavailable_total` | `SchedulerStalled`, `OpsMonitorDown`, `RateLimitCacheUnavailable` |

For **deep database analysis**, run `postgres_exporter` with
`pg_stat_statements` next to the managed instance; per-query statistics belong
there, not in the app.

All 24 rules live in `deploy/prometheus/alerts.yml` and are validated by
`promtool` in CI. `deploy/k8s/base/alerts.yaml` is generated from them with
`make k8s-alerts`. **Thresholds are starting points** chosen without production
traffic; re-baseline after two weeks of real load.

Locally: `docker compose --profile observability up -d` starts Prometheus on
http://localhost:9090. All 13 targets scraped successfully in this environment.

## Runbooks

| Alert | First checks | Likely causes / actions |
|---|---|---|
| `ApiErrorRateHigh` | `http.slow_or_failed` logs by route; Sentry; recent deploy (release tag) | bad deploy → roll back (deployment.md); DB down → `DatabaseConnectionsNearLimit` / readiness; 503s are expected while PostgreSQL is unavailable |
| `ApiLatencyP95High` | per-route `db_ms` vs total; pod CPU | CPU-bound: scale out (HPA max?); DB-bound: slow queries (pg_stat_statements), lock waits |
| `RateLimitCacheUnavailable` | Redis health | throttling is OFF (fail-open) until Redis returns; consider a WAF rate limit meanwhile |
| `DriverLocationsStale` | `GpsIngestionDelayed`; client release; Redis | apps not sending GPS (client bug or OS background limits), ingestion slow, Redis failover |
| `MatchingBacklog` / `MatchingLatencyHigh` | matching-worker CPU, replicas, `AssignmentConflictsHigh` | scale matching workers; hot-zone contention; ETA provider slow (`EtaLatencyHigh` → fallback ETA is automatic) |
| `OutboxBacklog` | relay pod logs; Kafka health | relay down (standby takes over via advisory lock), broker down (events wait safely), poison row → `OutboxEventsGaveUp` |
| `ConsumerLagHigh` | `manage.py event_pipeline_status`; consumer logs; DLQ | consumer down or slow; scale replicas (≤ partitions); poison messages go to `events.dlq.v1` |
| `PaymentsNeedReview` | `manage.py payments_status`; admin → Payment records (needs_review) | ambiguous outcome after max checks, provider contradiction, amount mismatch: compare with the provider dashboard, never re-charge blindly |
| `PaymentAttemptsStuck` | provider status page; `rh_payments_provider_timeout_total` | provider outage: attempts are reconciled automatically when it returns |
| `DatabaseConnectionsNearLimit` | connections by app (`pg_stat_activity.application_name`) | too many pods × workers: add PgBouncer (transaction pooling) or lower `GUNICORN_WORKERS`; leaks → `LongRunningTransaction` |
| `DatabaseDeadlocks` | PostgreSQL logs (deadlock detail) | a new code path violates the lock order Trip → MatchingJob → DriverProfile → RideOffer → Payment |
| `SchedulerStalled` | scheduler pod logs; advisory lock holder | stuck job (it has bounded DB timeouts), crashed pod (standby takes over) |

## Periodic jobs (`run_scheduler`)

| Job | Every | Purpose |
|---|---|---|
| `expire_stale_drivers` | 60 s | switch silent drivers offline (measured in dev: 4 stale "online" drivers corrected on the first run) |
| `reconcile_live_store` | 60 s | replay Redis-outage fallback writes, re-sync after Redis data loss |
| `prune_live_index` | 60 s | drop expired drivers from Redis H3 sets |
| `cleanup_realtime_presence` | 60 s | remove presence entries of dead sockets |
| `reconcile_trips` | 5 min | report trips whose dependants disagree (report-only; `--fix` is a human decision) |
| `prune_location_history`, `prune_event_tables` | daily | retention |

## Operational commands

| Command | Use |
|---|---|
| `make ops-monitor` | print current system gauges |
| `make events-status` | outbox, Kafka reachability, lag, consumer heartbeats |
| `make payments-status` | payment states, open attempts, review flags (exit 1 if any) |
| `make reconcile-trips ARGS=--fix` | repair safe trip inconsistencies |
| `make e2e` | full ride through every component (staging smoke) |
| `make drills` | **local only**: break Redis, Kafka, DB, workers and WebSockets, then verify |
| `make restore-drill` | backup → restore → verify |
