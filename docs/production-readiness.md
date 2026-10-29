# Production readiness (Step 10)

> **Verdict: not production-ready.** The backend is hardened, observable,
> load-tested and failure-tested *locally*, and its core invariants held under
> every test so far. But real money cannot be collected (sandbox payment
> provider only), push notifications are not delivered (sandbox), there is no
> client app, and nothing has run on production infrastructure, with production
> data volumes or real users. The blockers are listed below. `manage.py check
> --deploy` with `APP_ENV=production` **fails today on purpose**: error
> `rh.E013`, the sandbox payment provider.

## Production blockers

| # | Blocker | Why it blocks | Path |
|---|---|---|---|
| 1 | Only the **sandbox payment provider** exists | no money can be collected; `check --deploy` refuses it | implement `PaymentProvider` for a real PSP, including `lookup` by idempotency key and webhook signatures; certify in the PSP's test mode |
| 2 | Only the **sandbox push provider** exists | important alerts (driver arrived, payment failed) never reach phones | implement FCM / APNs / Web Push `PushProvider` |
| 3 | **No rider/driver client apps** (the Step 1 frontend was never built) | nothing for users to run; the end-to-end flow is exercised by scripts only | build clients against the API and WebSocket protocol (docs/realtime.md) |
| 4 | **Not run on the target platform** | the Kubernetes manifests are schema-validated, not deployed; managed PostgreSQL, Redis and Kafka are untested | stand up staging, run `make e2e`, the load tests and drills there, then a game day |
| 5 | **No independent security review / penetration test** | this review was done by the authors | external pen test (API, WebSocket, webhook endpoint, admin) before launch |
| 6 | **Legal / compliance** not done | surge pricing is an illustrative policy; driver location is personal data; payment data scope | legal review per market (surge disabled by default in Kubernetes); privacy assessment (retention, DSAR, deletion); PCI SAQ-A scope confirmation with the PSP |
| 7 | **Routing ETA** is a labelled straight-line fallback | pickup ETAs and fares are approximations | deploy OSRM/Valhalla or a routing API (`ETA_PROVIDER=osrm` exists) |

## Security review: what was found and fixed in this step

| Area | Finding | Fix | Verified by |
|---|---|---|---|
| Rate limiting | counters in per-process LocMem: limits did not hold across gunicorn workers or pods | Redis cache in deployed environments (`CACHE_BACKEND=redis`), **fail-open** wrapper so a Redis outage does not 500 every request | `test_rate_limiting_fails_open_when_redis_is_down`; Redis drill |
| Rate limiting | client IP from `REMOTE_ADDR`: behind a load balancer every user shares one bucket | `NUM_PROXIES` (set to 1 in Kubernetes); deploy check warns if unset | `test_client_ip_comes_from_the_trusted_proxy_hop` |
| Authentication | refresh tokens were irrevocable for 7 days; no logout | rotation plus blacklist (`ROTATE_REFRESH_TOKENS`, `BLACKLIST_AFTER_ROTATION`), `POST /auth/logout/` | `test_refresh_tokens_rotate_and_logout_revokes`; e2e smoke |
| Authentication | login throttled per IP only (distributed credential stuffing) | additional per-account throttle (`auth_email`, hashed key) | `test_login_attempts_are_limited_per_account_across_ips` |
| Authentication | JWT signing key defaulted to `SECRET_KEY` | deploy check requires a distinct, strong `JWT_SIGNING_KEY` | `test_deploy_checks_flag_unsafe_configuration` |
| Webhooks | unauthenticated endpoint had **no throttle**; unknown or disabled provider → **500** | per-IP throttle, 404 for unknown or disabled providers, 64 KB body cap → 413 | `test_webhook_unknown_or_disabled_provider_is_404_and_oversized_is_413` |
| Privacy | `/drivers/nearby/` returned each driver's **stable id + exact position**: any rider could track one driver around the city | per-rider, hourly-rotating HMAC pseudonyms; positions coarsened to about 100 m | `test_nearby_drivers_cannot_be_tracked_across_riders` |
| WebSockets | **no inbound rate limit**: one socket could flood the server | token bucket per connection (5 msg/s, burst 20) → `rate_limited`, then close **4429** | `test_inbound_flood_is_refused_then_disconnected` |
| Information exposure | public health endpoint exposed internal topology; Swagger UI public; admin at `/admin/` | `/health/live` + `/health/ready` (minimal); details staff-only; docs off when deployed; `ADMIN_URL_PATH` | health tests |
| Transport | no HSTS / referrer / COOP policy; SSL redirect would break probes | HSTS 1 year + subdomains, `SECURE_REDIRECT_EXEMPT` for probes and metrics, secure cookies, `X_FRAME_OPTIONS=DENY`; origins must be https (deploy check) | `check --deploy` in CI |
| Input | request bodies up to Django's default 2.5 MB | `DATA_UPLOAD_MAX_MEMORY_SIZE=256 KB` (ingress also caps 256k) | `test_oversized_json_bodies_are_refused` |
| Dependencies | **DRF 3.16.1**: 5 advisories including a bypass of `DATA_UPLOAD_MAX_MEMORY_SIZE`; **autobahn 24.4.2**: WebSocket payload-size bypass via compression | DRF → 3.18.x; autobahn ≥ 26.7.1 (image, Python 3.12) | pip-audit on the image's exact packages: 0 unignored |
| Containers | compose published PostgreSQL / Redis on all interfaces; image ran `migrate` on every web start (replicas race) | loopback-only ports; dedicated `migrate` role / Job; non-root uid 10001, read-only root FS, all capabilities dropped, Pod Security "restricted" | image smoke test (CI); kubeconform |
| Configuration | nothing stopped an unsafe production config | `APP_ENV` (DEBUG refused in staging/production at startup) and 20 deployment checks (`apps/common/checks.py`) run by the migrate Job | `test_production_refuses_debug`, `test_deploy_checks_flag_unsafe_configuration` |

Previously in place and re-verified in this step:

- **Ownership scoping**: another user's trip returns 404, verified in the e2e
  smoke.
- **Role checks** come from the database, never from the token.
- **WebSocket auth**: subprotocol token, Origin allow-list, per-message
  authorization.
- **Duplicate ride requests**: user row lock, partial unique index, and the
  `Idempotency-Key` replay (e2e: replayed 201 with `Idempotent-Replayed: true`,
  second active trip → 409).
- **Atomic driver reservations**: unique PENDING offer per driver; 0 double
  assignments across 1,704 load trips.
- **Webhook HMAC** verification.
- **No card data** stored or accepted (raw numbers → 400).

### Dependency advisories accepted (with reason)

| Advisory | Package | Why accepted | Until |
|---|---|---|---|
| CVE-2026-115877 | twisted 26.4 (HTTP header validation, request-smuggling class) | **Open risk.** Used by daphne. Mitigated by ingress-nginx terminating and re-issuing every request; the fix exists only as 26.10.0rc1 | upgrade to twisted 26.10.0 final when released |
| CVE-2026-106454, -115774, -115776, -115778, -115775, -83813 | twisted (IMAP, IRC, conch telnet, XML stream, DNS UDP) | those modules are never imported by this application | twisted upgrade |
| PYSEC-2026-4027 (local venv only) | autobahn on Python 3.10 | the fix needs Python ≥ 3.11; the image and CI use 3.12 | developers move local venvs to 3.12 |

## Reliability: verified behaviour

| Requirement | How it is handled | Evidence |
|---|---|---|
| DB transactions, atomic reservations | one transaction per state change; fixed lock order; unique constraints as the final guard | 675 tests (including real-thread concurrency); 0 invariant violations after load |
| Bounded timeouts | PostgreSQL `statement_timeout` 15 s, `lock_timeout` 5 s, `idle_in_transaction` 60 s, connect 5 s; Redis 0.25–0.5 s; Kafka delivery 10 s; ETA 1.5 s; payments via attempt + reconciliation; gunicorn 30 s | `test_lock_timeout_bounds_waiting_for_a_busy_trip` (503 in < 3 s, nothing half-applied) |
| Bounded retries | outbox (backoff, give up → FAILED + alert), consumers (4 retries → DLQ), payments (reconcile checks → review), notifications (5 → FAILED) | step 5/9 tests |
| Backpressure | gunicorn backlog; Kafka producer queue cap; channel-layer capacity; WebSocket token bucket; ingress `limit-rps`; DRF throttles | WS flood test |
| Graceful degradation | Redis → PostgreSQL fallback, throttling fails open; Kafka → outbox; DB down → 503 + Retry-After, liveness OK | `make drills`: 24/24 |
| Stale driver locations | freshness filter in matching; `expire_stale_drivers` (scheduler) | unit tests; dev run took 4 stale drivers offline |
| Expired offers | sweeper + re-match | 33 expired offers re-matched under load (performance.md) |
| WebSocket disconnects | heartbeat timeout; reconnect resync | WebSocket server SIGKILL drill |
| Payment timeouts | attempt UNKNOWN → reconciliation by idempotency key | drill: PAID after 32 s, 1 charge |

## Verification summary (this environment, 2026-10-29)

| What | Result |
|---|---|
| Automated tests (PostgreSQL 16, Redis 7.4, Kafka 3.9) | **675 passed, 0 failed** (88 s) |
| Local CI pipeline (`make ci`) | **passed**: lint, image dependency audit, fresh-DB migrations + `migrate --check`, staging `check --deploy`, tests, image smoke, 24 alert rules, 39/39 Kubernetes resources per overlay, compose |
| End-to-end smoke (`make e2e`) | **13/13 steps passed** in 2.7 s, through every worker |
| Failure drills (`make drills`) | **24/24 checks passed** |
| Backup → restore drill | **passed** (8 tables identical, invariants equal) |
| Load tests | see performance.md: measured on one laptop, **no capacity claims** |
| GitHub Actions workflow | written, YAML-valid, steps mirrored locally by `make ci`; **never executed on GitHub** (no runner available here) |
| Trivy image scan | in CI only; **not run locally** |

## Known limitations

- **Single region.** Partitioning strategy documented (performance.md), not
  implemented.
- **Rotating `JWT_SIGNING_KEY` logs everyone out** (no key ids). Access tokens
  live 15 minutes, so a revoked refresh token stops working within that window.
- **Registration responses reveal whether an email exists.** This is rate
  limited; a confirm-by-email flow would remove it.
- **Email/password only.** No MFA for staff, and no email verification or
  password reset.
- **One relay publishes all outbox events.** Fine for the measured volumes;
  partition it if it ever becomes the bottleneck.
- **Load numbers come from one machine** with the generator co-located.
  WebSocket servers were not tested beyond 2 processes.
- **PostgreSQL is the scaling ceiling** (a single writer). Add PgBouncer before
  pods × workers approach `max_connections`.
- **Local Python 3.10 venvs** cannot install the patched autobahn.
- **Grafana dashboards** are not included; the alert rules and metric catalogue
  are (operations.md).
- **Twisted CVE-2026-115877** stays open until 26.10.0 final; ingress
  mitigation in place.
