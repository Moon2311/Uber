# Deployment, rollback and recovery

## Platform

**Kubernetes** (any conformant cluster, v1.28 or later: EKS, GKE, AKS…) runs the
application. **Managed** PostgreSQL 16, Redis 7 and Kafka 3.x (RDS / Cloud SQL,
ElastiCache / Memorystore, MSK / Confluent) run the data services. Running
stateful data services inside the cluster is deliberately not part of this
design.

This is a choice, not a requirement of the code. The same image runs anywhere
that can run containers with environment variables. Docker Compose
(`docker-compose.yml`) is for **local development only**.

```
deploy/
  k8s/base/              Deployments, Services, HPAs, PDBs, Jobs, Ingress (TLS),
                         NetworkPolicies, PodMonitor + PrometheusRule
  k8s/overlays/staging   1 replica, sandbox providers allowed, staging hosts
  k8s/overlays/production
  prometheus/            scrape config + alert rules (single source of truth)
  scripts/               ci-local.sh, backup_restore_drill.sh
```

### One image, many roles

`backend/Dockerfile` builds the image:

- multi-stage, Python 3.12, non-root uid 10001;
- read-only root filesystem compatible (only `/tmp` is writable);
- hashed static files baked in;
- tagged with an immutable git sha.

The role is the first argument, interpreted by `docker-entrypoint.sh`:

| Role | Runs |
|---|---|
| `web` | gunicorn (`gunicorn.conf.py`: bounded backlog, timeouts, worker recycling) |
| `asgi` | daphne (WebSockets) |
| `migrate` | `check --deploy` (blocks unsafe configuration) + `migrate` |
| `<command>` | any long-running worker, e.g. `run_matching_worker` |

Migrations **never** run on web start: replicas starting together would race.

## Prerequisites (once per environment)

1. **Data services.** PostgreSQL with TLS, PITR enabled and multi-AZ; Redis with
   TLS and `maxmemory-policy noeviction`; Kafka with RF=3 and TLS/SASL.
2. **Ingress.** ingress-nginx with cert-manager (or your controller); DNS for
   `api.` and `ws.` hosts.
3. **Monitoring.** kube-prometheus-stack, so the PodMonitor and PrometheusRule
   in `base/monitoring.yaml` and `base/alerts.yaml` apply.
4. **Secret.** Create `app-secrets` from your secret manager; the keys are listed
   in `base/secret.example.yaml`. `JWT_SIGNING_KEY` must differ from
   `DJANGO_SECRET_KEY`.
5. **Nodes** allow `nofile` ≥ 65536 per container. This was measured as required
   for WebSocket density (performance.md).

## HTTPS / WSS requirements

- TLS terminates at the ingress. Clients use **https://** and **wss://** only.
  Bearer tokens travel in headers (HTTP) and in the WebSocket subprotocol (WSS),
  never in URLs.
- The ingress must send `X-Forwarded-Proto`. Django trusts it via
  `SECURE_PROXY_SSL_HEADER`, redirects http→https (probe paths exempt) and sets
  HSTS for 1 year.
- `NUM_PROXIES=1` (one LB hop) makes rate limiting use the real client IP. If
  this is wrong, every client shares one bucket, or spoofed `X-Forwarded-For`
  is trusted.
- `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS` and `REALTIME_ALLOWED_ORIGINS`
  must be `https://` origins: `check --deploy` refuses anything else.
  WebSocket idle timeouts at the LB must exceed the heartbeat (25 s); the
  ingress here sets 3600 s.
- `/metrics` is not routed publicly (ingress returns 404). Prometheus scrapes
  pods directly with `METRICS_TOKEN`.

## Routine deploy (what CI/CD does)

1. CI passes (`.github/workflows/ci.yml`):
   - lint and the dependency audit,
   - migration checks on an empty database plus `check --deploy`,
   - 675 tests against real PostgreSQL, Redis and Kafka,
   - image build plus a non-root read-only smoke test and a Trivy scan,
   - manifest and alert-rule validation.
2. Push the image as `…/ride-hailing-backend:<git-sha>`.
3. Set the image on the overlay:
   `cd deploy/k8s/overlays/production && kustomize edit set image ghcr.io/example/ride-hailing-backend=…:<git-sha>`
4. **Two phases**: Jobs first, Deployments only after the migration succeeded.
   A single `kubectl apply -k` would roll the Deployments out at the same moment
   as the Job, so a failed migration would not stop the new image.
   ```
   O=deploy/k8s/overlays/production
   kubectl -n ride-hailing delete job migrate kafka-topics --ignore-not-found
   kustomize build $O | kubectl apply -f - -l 'app.kubernetes.io/name in (migrate,kafka-topics)'
   kubectl -n ride-hailing wait --for=condition=complete job/migrate --timeout=30m   # fails -> STOP
   kubectl -n ride-hailing wait --for=condition=complete job/kafka-topics --timeout=5m
   kustomize build $O | kubectl apply -f -
   kubectl -n ride-hailing rollout status deploy/api deploy/realtime --timeout=10m
   ```
   If `job/migrate` fails (failed migration, or `check --deploy` refused the
   configuration), nothing else has changed: the old pods keep serving.
5. Verify:
   - `make e2e` against staging, or the production smoke (synthetic account);
   - the error-rate and latency alerts stay quiet for 15 minutes.

Rollouts are rolling with `maxUnavailable: 0`. API pods get `preStop: sleep 5`
and the realtime pods `sleep 10`, so the load balancer drains them before
SIGTERM. Workers finish their current iteration on SIGTERM (60 s grace).

## Migrations policy: expand → migrate → contract

The previous image must keep working against the new schema (rolling deploys run
both versions together, and a rollback runs the old one).

| Change | Release N | Release N+1 |
|---|---|---|
| add column | add as nullable / with default; code tolerates both | backfill, then add `NOT NULL` |
| remove column | stop reading and writing it | drop it |
| rename | add the new column, write both, read the new one | drop the old one |
| new constraint | `NOT VALID` + `VALIDATE CONSTRAINT` separately (no long lock) | |
| big backfill | separate management command, batched; not inside `migrate` | |

The migrate role runs with `statement_timeout=0` and `lock_timeout=10s`, so a
migration that cannot get its lock fails fast instead of queueing every request
behind it.

Migrations are **forward-only** in production. Several data migrations have
`reverse_sql=noop`. A bad migration is fixed with a *new* migration, or with a
PITR restore when data was destroyed (disaster-recovery.md).

## Rollback

| What broke | Do this | Time |
|---|---|---|
| New image misbehaves (errors, latency) | `kustomize edit set image …:<previous-sha>` + `kubectl apply -k …`, or `kubectl -n ride-hailing rollout undo deploy/<name>` for every Deployment | minutes |
| Bad configuration (ConfigMap / Secret) | revert the config commit / secret version, apply, `kubectl rollout restart` (pods read env at start) | minutes |
| Migration that breaks the old image | should not happen (expand/contract). If it does: forward-fix migration, or roll the image *forward* to a fixed build | |
| Data damaged by a release | stop writers, PITR to before the release (disaster-recovery.md), **reconcile payments with the provider** | ~1 h |
| Surge or pricing rule mistake | `pricing_rules publish <previous rules.json>` (new version = old content; versions are immutable), or `PRICING_SURGE_ENABLED=false` | seconds |
| Kafka consumer bug corrupting a projection | stop the group, fix, reset offsets to before the bug (`kafka-consumer-groups --reset-offsets --to-datetime`) and replay; handlers are idempotent per event | |

Keep the previous image tag in the release notes of every deploy. Rollback
always means *deploy a known-good sha*, never `:latest`.

## Secrets rotation

| Secret | How |
|---|---|
| `DJANGO_SECRET_KEY` | change and restart. Admin sessions are invalidated; nothing else depends on it (JWTs use their own key) |
| `JWT_SIGNING_KEY` | change and restart. **All tokens become invalid**, so every user logs in again. Plan it, or add a key-id scheme first (known limitation) |
| `PAYMENTS_WEBHOOK_SECRETS` | add the new secret *first* (`new,old`), switch the provider to sign with the new one, then remove the old one |
| `METRICS_TOKEN` | update the secret; Prometheus reads the same secret |
| Database / Redis passwords | managed-service rotation with dual users, then update the URL and restart |

## Environments

| Setting | development | staging | production |
|---|---|---|---|
| `APP_ENV` | development | staging | production |
| DEBUG | allowed | refused at startup | refused at startup |
| `check --deploy` | n/a | run by the migrate Job (warnings OK) | run by the migrate Job (**errors block**) |
| Rate-limit cache | locmem | Redis | Redis |
| Metrics | log lines | Prometheus | Prometheus |
| Payments | sandbox | sandbox (`PAYMENTS_SANDBOX_ALLOWED=true`) | **real provider required** (`check --deploy` blocks the sandbox) |
| API docs | on | off | off |
| Surge | on (dev) | off by default | off until legal review per market |
