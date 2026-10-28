# Pricing, payments, fraud and notifications (Step 9)

> **Legal notice: surge pricing is an illustrative policy.** The demand/supply
> multiplier in this repository shows *how* dynamic pricing can be implemented
> safely. It is **not** a recommendation, and it has not been reviewed for any
> jurisdiction. Many places regulate or forbid it: caps on multipliers, bans
> during declared emergencies (price-gouging laws), disclosure and up-front
> consent requirements, rules for licensed taxi or for-hire services, and
> consumer-protection and anti-discrimination law. Before real riders are
> charged, operators must get legal review for each market. They must then
> configure the cap (`PRICING_SURGE_HARD_CAP`, `surge.max_multiplier`), disable
> it where required (`PRICING_SURGE_ENABLED=false`, `surge.enabled: false`, or
> `PRICING_SURGE_DISABLED_ZONES`), and show the multiplier to the rider before
> they accept. The quote already carries it.

| Area | Code | Runs in |
|---|---|---|
| Pricing rules, quotes, surge | `apps/pricing` | API (quotes), `update_surge` worker |
| Payments | `apps/payments` | API, `run_payments_worker`, webhook endpoint |
| Fraud | `apps/fraud` | `fraud` Kafka consumer group (monitoring), API guards (blocking) |
| Notifications | `apps/notifications` | `notifications` consumer group, `run_notification_worker` |

## 1. Pricing

### Versioned rules

`PricingRuleSet` rows hold the rates per category (`base`, `per_km`,
`per_minute`, `minimum`, `booking_fee`), the distance model and the surge
policy, as JSON validated by `pricing/rules.py`.

- **Immutable.** A published version cannot be edited; `save()` refuses.
  `manage.py pricing_rules publish rules.json` validates the document, retires
  the active version and activates a new one in a single transaction. A partial
  unique index guarantees exactly one ACTIVE version.
- A fresh database is bootstrapped with the built-in defaults as **v1**.
- Inspect versions with `pricing_rules list` and `pricing_rules show [version]`.

### Quotes (upfront price)

`POST /api/v1/pricing/estimate/` returns a **FareQuote**:

```
amount = max(base + per_km·road_km + per_minute·minutes + booking_fee, minimum) × surge
```

The quote stores the full breakdown, the `pricing_version` and the surge
multiplier, and is valid for `PRICING_QUOTE_TTL_SECONDS` (300 s).

`POST /trips/` with `quote_id` makes the trip ride at exactly that price.
`accept_quote` checks that the quote belongs to the rider, has not expired, and
matches the same category and points. A `OneToOne` from `Trip.fare_quote` makes
a quote usable once. A request without `quote_id` gets a fresh quote, so older
clients keep working.

The trip records `fare_quote`, `pricing_version` and `surge_multiplier`, and
`estimated_fare` equals the quote amount. That amount is what the payment
charges, and it is reported as `final_fare` in `TripCompleted`.

Distance and duration are still a straight-line estimate scaled by
`road_factor`. A routing engine would replace `DistanceTimeFareEstimator`.

### Surge (illustrative)

`update_surge` runs every 30 s and writes one `SurgeZone` row per zone (H3
resolution 7, about 5 km²). Each row records its inputs.

- **Demand** is the number of *distinct riders* who got a quote in the zone
  during `window_seconds`. One rider refreshing the price screen counts once.
- **Supply** is the number of online drivers in the zone with a fresh fix and
  no active trip.
- **Target multiplier** is 1.0 while demand is below `min_demand`. Above that
  it is `1 + sensitivity · (demand/supply − ratio_threshold)`, capped at
  `max_multiplier`. `max_multiplier` itself can never exceed
  `PRICING_SURGE_HARD_CAP`.
- **Smoothing and rounding.** The multiplier moves at most
  `max_change_per_update` per run, so it neither jumps nor oscillates, and it is
  rounded **down** to the `step`.
- **Fail-safe.** A value older than `stale_after_seconds` (for example because
  the worker stopped) means **no surge**, never a stuck surge. Zones decay back
  to 1.0 once demand subsides.

## 2. Payments

### States

```
PENDING ──► PROCESSING ──► PAID ──► REFUNDED
   │            │  ▲  │
   │            ▼  │  └─► PENDING   provider unreachable: nothing was sent
   │          FAILED                 (retried later)
   └─► CANCELLED                     PENDING ──► PAID   cash at completion
```

- Transitions are compare-and-set on `version` (`services._transition`), and
  every change emits `PaymentStatusChanged` through the outbox.
- `Trip.status` and `PaymentRecord.status` are independent. Payments follow
  trip signals inside the trip transaction (create on request, cancel on
  cancellation, mark due on completion). Trips never read payments, except
  through the **blocking guard**: a FAILED payment means the rider must settle
  it before requesting again (402).

### The attempt pattern (timeouts and ambiguous outcomes)

Every call that moves money is a `PaymentAttempt`, handled in three steps:

1. **Commit the intent.** In a transaction, lock the payment and create the
   attempt with a deterministic key (`pay_<id>_charge_<n>`), move the payment to
   PROCESSING, and **commit**.
2. **Call the provider** with that idempotency key, outside any transaction.
3. **Apply the answer** in a transaction that locks the payment and the attempt.
   The result is applied only if the attempt is still open, so this step is
   idempotent.

| Provider answer | Result |
|---|---|
| succeeded / failed | PAID / FAILED |
| pending (asynchronous) | attempt PENDING; settled by webhook or reconciliation |
| timeout / lost response | attempt **UNKNOWN**; payment stays PROCESSING |
| connection refused | not processed; payment back to PENDING and retried later |
| request rejected | FAILED and flagged for review |

`reconcile_open_attempts` runs in the payments worker. For open attempts that
are due, it asks the provider `lookup(idempotency_key)`:

- **Provider has a result:** apply it.
- **Provider never received the request:** re-send with the **same key**.
- **Still unknown after `PAYMENTS_MAX_RECONCILE_CHECKS`:** set `needs_review`
  and stop checking automatically.

A partial unique index (`attempt_one_open_per_payment`) makes it impossible to
start a second charge while an earlier one might have taken money. `make
payments-status` lists open attempts and payments needing review, and exits 1
if any need review.

### Webhooks

`POST /api/v1/payments/webhooks/<provider>/` takes no user authentication: the
signature is the authentication.

- `Payment-Signature: t=<unix>,v1=<hmac>` is HMAC-SHA256 over
  `"<t>." + raw body`. It is verified on the **raw bytes before parsing**, with
  a constant-time compare.
- Requests outside a ±300 s window are rejected.
- Several secrets are accepted (`PAYMENTS_WEBHOOK_SECRETS`), so secrets can be
  rotated without downtime.
- With no secret configured, every webhook is rejected (fail closed).
- **Duplicates.** `WebhookEvent` is unique on (provider, event id): a retried
  delivery returns `{"status": "duplicate"}` and changes nothing. The same fact
  arriving under a new event id gets `no_change`.
- **Contradictions** (for example "failed" after "succeeded") and amount or
  currency mismatches are **flagged, never applied**.

### Refunds and retries

- **Refund.** `POST /payments/{id}/refund/` (staff only) issues a full refund
  through the same attempt pattern. It is idempotent: one successful refund per
  payment.
- **Retry.** `POST /payments/{id}/retry/` lets the rider retry a FAILED payment,
  optionally with another card. Attempts are capped at
  `PAYMENTS_MAX_CHARGE_ATTEMPTS`.

### Card data and secrets

- Cards are tokenized **client-side** with the provider SDK, and the API takes
  only `provider_token`.
- Card-number-shaped input and fields such as `cvc` are rejected (400), and the
  rejected value is never echoed or logged.
- Stored: the provider's reusable method id (never returned by the API, never
  logged, hidden in admin), plus brand, last4 and expiry. PCI DSS allows these
  outside the cardholder-data environment.
- Webhook secrets come from the environment and are never logged.
- Riders never see provider references or review flags.
- The sandbox provider is refused when `DEBUG=false` unless
  `PAYMENTS_SANDBOX_ALLOWED=true`.

### Sandbox provider

Test tokens choose the behaviour. `tok_sandbox_visa` and
`tok_sandbox_mastercard` succeed. These simulate failures:

| Token | Behaviour |
|---|---|
| `tok_sandbox_decline` | declined |
| `tok_sandbox_insufficient_funds` | declined (insufficient funds) |
| `tok_sandbox_async` | pending; settled by webhook |
| `tok_sandbox_timeout` | **charged, but the response is lost** |
| `tok_sandbox_lost_request` | request never arrives; a retry with the same key succeeds |
| `tok_sandbox_unavailable` | provider unreachable |

Attaching a token mints a reusable `pm_sandbox_…` id, as real providers do. The
sandbox keeps its state in `SandboxCharge`, so idempotency and lookups behave as
they would with a real provider.

## 3. Fraud

### Asynchronous monitoring

The `fraud` consumer group reads `trip.events.v1`, `payment.events.v1` and the
sampled `driver.location.v1`. It records `RiskSignal`s and recomputes a
`RiskProfile`. It opens a `FraudCase` for human review when the level reaches
ELEVATED or HIGH, but **it never blocks anyone by itself**. Redelivery is
harmless because of the ProcessedEvent ledger and a unique (event, signal,
subject) key.

Signals are configured in `FRAUD_RULES` (weight, reliability, TTL, thresholds):

| Signal | Subject | Source | Reliability |
|---|---|---|---|
| `gps_impossible_speed` | driver | consecutive sampled fixes > 250 km/h over ≥ 1 km | **LOW** |
| `implausible_trip_speed` | driver | route km vs server-side start/complete times | MEDIUM |
| `rider_rapid_cancellations` | rider | ≥ 4 rider cancellations in 1 h (PostgreSQL `cancelled_by`) | MEDIUM |
| `repeated_rider_driver_pair` | both | ≥ 4 completed trips by the same pair in 24 h | MEDIUM |
| `payment_failed` / `repeated_payment_failures` | rider | declines; our own outages are ignored | HIGH |

**Score.** For each signal type, points are summed and capped at
`max_points_per_signal`, so one noisy signal cannot dominate. The total is
capped at 100. `blocking_score` is the same calculation **without LOW-reliability
signals**.

### Blocking checks

Blocking checks run synchronously (`ACTION_GUARDS`) on `trip_request` and
`add_payment_method`. The decision is:

- **BLOCK** requires one of the following:
  - a reviewer's manual hold (Django admin → Fraud cases → *Place a hold*);
  - `blocking_score ≥ 70` from at least **2 distinct** reliable signal types;
  - a deterministic hard limit (card-testing velocity: 5 methods per hour).
- **REVIEW** (allowed, recorded): `score ≥ 40`.
- **ALLOW** otherwise.

**A single, or repeated, unreliable GPS anomaly can never block.** GPS signals
are LOW reliability, capped at 30 points, and excluded from `blocking_score`.
GPS is spoofable and noisy (tunnels, multipath, cold starts), so it only ever
leads to human review.

Every decision is stored in `FraudDecision`. A BLOCK is recorded *after* the
denied request's transaction rolls back (`record_after_rollback`), so blocks
are never lost from the audit. Responses are a generic 403 `action_blocked`
that reveals neither the score nor the signal.

**If the check itself fails**, behaviour is set per action in
`FRAUD_POLICY["fail_open"]`. Trip requests are allowed, because the payment
still has to succeed. Adding a payment method is refused (503), because that is
the card-testing target.

## 4. Notifications

| Channel | Used for |
|---|---|
| **Push** (`apps/notifications`) | Important alerts that must reach a phone in a pocket: driver assigned, driver arrived, driver released, no driver found, trip cancelled, new ride offer (driver), payment receipt, payment failed, refund |
| **WebSocket** (`apps/realtime`) | Live in-app updates: every trip status, driver position, approaching, offers, and now `payment.status` |

- **Creation.** The `notifications` consumer group turns events into
  `Notification` rows. `dedupe_key = <event_id>:<kind>:<user>` is unique, so a
  redelivered event notifies once.
- **No personal data.** Templates carry no names or addresses; payloads contain
  ids only.
- **Delivery.** `run_notification_worker` claims due rows (SKIP LOCKED plus a
  lease) and sends to every active device through the `PushProvider`
  abstraction (sandbox now; FCM, APNs or Web Push later). Each device's outcome
  is recorded in `NotificationDelivery`.

| Push-service result | Handling |
|---|---|
| Transient failure | Exponential backoff up to `NOTIFICATIONS_MAX_ATTEMPTS`, then FAILED. Devices that already received the notification are not sent it again. |
| Invalid token | Device deactivated |
| Rejected payload | FAILED, no retry |
| Alert past its TTL (e.g. a 60 s ride offer) | SKIPPED, not delivered late |
| User has no devices | SKIPPED |
| Worker crashes mid-send | Retried after the lease expires (at-least-once; collapse key per trip) |

The inbox is `GET /api/v1/notifications/`, and devices register with
`POST /api/v1/notifications/devices/`.

## 5. Who owns what

| Record | Owner | Notes |
|---|---|---|
| Pricing rule versions, quotes, surge history | PostgreSQL | quotes reference the exact rule version |
| Payment records, attempts, webhook log | PostgreSQL (our belief) | the **provider** is the authority on money; reconciliation closes the gap |
| Saved payment methods | PostgreSQL (tokens only) | card data lives only at the provider |
| Risk signals and profiles | PostgreSQL, **derived** from Kafka | rebuildable by replay |
| Holds and case decisions | PostgreSQL, **authoritative** | human decisions |
| Notifications and deliveries | PostgreSQL | push services are fire-and-track |

Nothing here is a dual write:

- Provider calls happen **after** the intent is committed and are reconciled by
  idempotency key.
- Kafka is fed only through the outbox.
- WebSocket pushes happen after commit.

## 6. Deploying this step

1. `migrate`. Legacy `CAPTURED` payments become `PAID`; `AUTHORIZED` ones become
   `PENDING` and are flagged for review.
2. Set `PAYMENTS_WEBHOOK_SECRETS`. Webhooks are rejected until it is set.
3. Deploy consumers (`fraud`, `notifications`, `analytics`) **before** the API
   and relay start emitting `PaymentStatusChanged`, because an old consumer
   dead-letters unknown event types. Run `make topics` to create
   `payment.events.v1`.
4. Start `surge-updater`, `payments-worker` and `notification-worker` (all are
   safe to run as several replicas).
5. Decide the surge configuration per market **with legal review** (see the
   notice at the top) before enabling it with real riders.

## Known limitations

- Only the sandbox payment provider exists. A real integration must implement
  `PaymentProvider`, including `lookup` by idempotency key.
- Only full refunds are supported. There is no tipping and no fare adjustment
  after the trip: fares are upfront.
- Cash is recorded as PAID at completion. There is no driver cash-collection
  confirmation.
- Fraud thresholds are starting points and must be tuned against real data.
  There is no ML model.
- Push uses a sandbox provider. There are no user notification preferences or
  quiet hours yet.
