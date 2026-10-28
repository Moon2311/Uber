# Real-Time Communication (Step 7)

Riders and drivers keep a WebSocket open to receive ride offers, trip status,
driver location and milestones as they happen, and drivers answer offers over
the same socket. "RAMEN" from the reference diagram is only a conceptual label.
The implementation is a plain **Django Channels** consumer served by **Daphne**
(ASGI), with a **Redis channel layer** so any number of WebSocket instances can
share fan-out.

Code: `backend/apps/realtime/`.

## WebSockets vs Kafka

Both are fed by the same domain signals, but they are **independent paths** with
different jobs. Neither reads from the other.

| | WebSockets (`apps/realtime`) | Kafka (`apps/events`) |
|---|---|---|
| Audience | The rider's and driver's **apps** that are connected right now | Other **services** (analytics now; billing, fraud, notifications later) |
| Purpose | Low-latency UI updates (ms) | Durable, replayable record of business facts |
| Delivery | At-most-once, ephemeral; lost if nobody is connected | At-least-once via the outbox; retained 7 days |
| Ordering / recovery | No replay: a reconnecting client receives a **snapshot** of current state | Consumers resume from committed offsets; idempotent by `event_id` |
| Trigger | `transaction.on_commit` → `channel_layer.group_send` | Outbox row in the same transaction → relay → topic |
| Transport | Redis channel layer (pub/sub between ASGI instances) | Kafka brokers |
| Failure impact | Redis down → pushes skipped (metric); clients recover on reconnect/sync | Kafka down → events wait in the outbox |
| Source of truth? | **No** | **No** |

PostgreSQL is the source of truth for both. Pushing UI updates through Kafka
would add broker latency and a consumer hop. Using WebSockets for inter-service
facts would lose durability. So the paths stay separate.

## Architecture

```
            business code (trips, matching, drivers, locations)
                  │  sends Django signals inside its transaction
       ┌──────────┴───────────┐
apps/events/receivers   apps/realtime/notifier            ← delivery only
  outbox row (same txn)   on_commit → group_send(Redis channel layer)
       │                        │
   relay → Kafka        Daphne #1 ─┐  ┌─ Daphne #2      (any instance)
                          sockets  └──┘  sockets
```

- **Business modules never import Channels.** The notifier only *listens*.
- **Pushes fire after COMMIT** (`transaction.on_commit`). A rolled-back
  assignment is never announced (tested).
- **Payloads are re-read from PostgreSQL** after commit (`_fresh_snapshot`).
  They are never built from in-memory objects, and they are JSON-native (decimals
  as strings), so they are also safe for the msgpack-encoded Redis layer.
- **A push failure is caught** and metered (`realtime.push_failed`). It can never
  undo or fail a business operation.

## Connecting

```js
const ws = new WebSocket("wss://api.example.com/ws/v1/", ["jwt", accessToken]);
```

- **Authentication.** The JWT **access** token goes in the subprotocol list, not
  the URL, because URLs end up in access logs. The server answers with the `jwt`
  subprotocol, loads the user from PostgreSQL (role from the DB, never the token
  claims) and rejects the handshake with HTTP 403 if the token is missing,
  invalid, expired, a refresh token, or belongs to an inactive user.
- **Origin.** Browser origins must be in `REALTIME_ALLOWED_ORIGINS` (defaults to
  `CORS_ALLOWED_ORIGINS`); a foreign origin gets close code 4403. Native apps send
  no `Origin`, which is allowed unless `REALTIME_ALLOW_MISSING_ORIGIN=false`.
  Because auth is token-based rather than cookie-based, a malicious page can't
  open an authenticated socket on a victim's behalf.
- **Token expiry mid-connection.** The socket gets `{"type":"error","code":"token_expired"}`
  and is closed with **4401**. The client refreshes its token and reconnects.

## Protocol

Client → server, as JSON objects. An optional `id` is echoed back as `reply_to`.

| type | fields | effect |
|---|---|---|
| `ping` | — | `pong` (application heartbeat; send every `heartbeat_interval_s`) |
| `sync` | — | resends snapshots of active trips plus pending offers, then `synced` |
| `subscribe.trip` | `trip_id` | **authorized** join of the trip channel, then `subscribed` and `trip.snapshot` |
| `unsubscribe.trip` | `trip_id` | leave |
| `offer.accept` / `offer.decline` | `offer_id` | **validated in PostgreSQL**; reply `offer.result` |

Server → client:

| type | to | when |
|---|---|---|
| `welcome` | the connection | on connect (connection id, heartbeat settings, server time) |
| `trip.snapshot` | the connection | on connect, on `sync`, on subscribe, on server-driven join |
| `synced` | the connection | end of a resync (`trips`, `pending_offers`) |
| `offer.created` | **the selected driver only** (`driver.<id>`) | after the offer is committed; `replayed: true` on reconnect |
| `offer.closed` | that driver | declined, expired, cancelled (trip cancelled / driver offline) or accepted on another device |
| `offer.result` | the responding connection | authoritative outcome of accept/decline |
| `trip.status` | `trip.<id>` (rider and assigned driver) | every committed status change, with `milestone` and a fresh `trip` snapshot |
| `trip.driver_location` | `trip.<id>` | each GPS fix of the **assigned** driver while assigned / arriving / in progress |
| `trip.driver_approaching` | `trip.<id>` | once, when the assigned driver comes within `REALTIME_APPROACHING_RADIUS_M` (300 m) of the pickup |
| `error` | the connection | `unknown_type`, `invalid_json`, `invalid_message`, `invalid_trip_id`, `invalid_offer_id`, `not_found`, `forbidden`, `token_expired` |

The rider-facing `milestone` values are `searching`, `driver_assigned`,
`driver_arriving`, `trip_started`, `trip_completed` and `trip_cancelled`. Drivers
move a trip forward with `POST /api/v1/trips/{id}/arrive/`, `/start/` and
`/complete/`. These are new in this step: assigned driver only, idempotent, and
validated against the state machine.

Every snapshot has a `version` (the trip's `updated_at`), so clients drop
anything older than what they already show.

## Authorization

- **Groups are never chosen by the client.** The server joins each connection
  to `user.<id>`, plus `driver.<driver_id>` for drivers.
- **Trip channels** (`trip.<id>`) are joined only after `trip_for_user` succeeds,
  which allows the trip's rider or its **assigned** driver.
  - A driver who was merely *offered* a trip gets the offer payload (route and
    fare, no rider details) and can't join the trip channel.
  - Non-existent and not-yours trips get the same `not_found`, so existence
    isn't revealed.
- **Server-driven joins** (a trip just created, a driver just assigned) are sent
  as control messages to `user.<id>`. The consumer **re-authorizes against
  PostgreSQL** before joining.
- **Clients can't inject server events.** Sending `trip.status`, `push` or
  `group.join` gets `unknown_type`, and nothing changes.
- **Messages over `REALTIME_MAX_MESSAGE_BYTES` (4 KB)** close the socket with 4413.

## A WebSocket message is never proof of assignment

- `offer.accept` over WebSocket runs **exactly the same transactional
  `accept_offer`** as the HTTP API. That means row locks (Trip → Job → Driver →
  Offer), expiry checks and DB constraints (Step 6).
- The `offer.result` reply is built from the offer and trip **re-read from
  PostgreSQL** afterwards (`offer_status`, `trip_status`, `assigned_to_me`), never
  from what the client sent or what the call "should" have done. A test
  substitutes a no-op `accept_offer`; the reply still reports `PENDING`.
- The rider is told `driver_assigned` only by the post-commit notifier, based on
  the committed trip row.

## Heartbeats, disconnects, reconnects, stale connections

| Concern | Mechanism |
|---|---|
| **Transport keep-alive** | Daphne sends WebSocket protocol pings every 20 s and drops peers silent for 30 s (`--ping-interval/--ping-timeout`). |
| **Application heartbeat** | The client sends `ping` every 25 s. A watchdog closes the socket with **4408** if nothing arrives within 60 s, which catches half-open connections the transport misses (tested). |
| **Disconnect** | `disconnect()` cancels the watchdog, leaves every group and removes the presence entry. Abrupt drops (code 1006) take the same path (verified live). Pending offers are **not** cancelled on disconnect: the driver may be back within seconds, and the 15 s offer expiry already guarantees progress. |
| **Reconnect** | The client reconnects with backoff (for example 0.5 s doubling to 30 s, with jitter) to **any** instance. On connect the server re-joins active trips and sends their snapshots, and **replays pending offers** (`replayed: true`). State is transferred, not replayed from a log, so the client converges after any gap (tested; live: driver reconnected to the other instance). |
| **Stale cleanup** | Each watchdog tick re-adds group memberships and refreshes presence. The channel layer's `group_expiry` (300 s) therefore removes memberships left by a **crashed** instance, and `manage.py cleanup_realtime_presence` drops presence entries not refreshed within the heartbeat timeout. |

## Offer expiry and rematching

This is unchanged from Step 6 and now visible in real time:
- The worker's sweeper expires unanswered offers (15 s) under row locks.
- The driver receives `offer.closed {status: EXPIRED, reason: timeout}`.
- The job is woken and the next ranked driver receives `offer.created`.
- A late accept from the first driver is refused by the backend with
  `ok:false, offer_status: EXPIRED` (tested).

## Scaling: Redis channel layer

`CHANNEL_LAYER_BACKEND=redis` (the default) uses `channels_redis` with prefix
`rh:ws:`, a per-channel capacity of 1000 messages (slow clients drop, not grow),
a 60 s message expiry and a 300 s group expiry.

Pushes come from whichever process commits the change: the API (gunicorn), the
matching worker or a Daphne instance. All of them publish into the same layer,
so a rider on instance 1 hears about an accept that happened on instance 2.
`memory` is for single-process tests only.

Docker Compose runs **two** WebSocket instances, `realtime-1` on :8001 and
`realtime-2` on :8002. In production they sit behind a load balancer; sticky
sessions are not required.

## Metrics

| Metric | Notes |
|---|---|
| `realtime.connected{role}` / `realtime.disconnected{code}` | connection lifecycle |
| `realtime.connect_rejected{reason}` | `missing_token` / `invalid_token` / `unknown_user` |
| `realtime.heartbeat_timeout` | sockets closed by the watchdog |
| `realtime.inbound{type}` | client messages by type |
| `realtime.subscribe_denied` | unauthorized trip subscriptions |
| `realtime.offer_response{accept,ok}` | offer answers over WebSocket |
| `realtime.push{event}` / `realtime.push_failed{event}` | outbound pushes |
| `realtime.redis_error` | Redis errors |

Structured logs: `realtime.connected` / `realtime.disconnected` (`user_id`,
`connection_id`, `code`) and `realtime.push_failed`.

## Testing

`backend/apps/realtime/tests/` contains 42 tests driving the real ASGI app
(including the auth and origin middleware) with Channels' `WebsocketCommunicator`.

| File | Covers |
|---|---|
| `test_auth.py` | JWT subprotocol; missing/invalid/expired/refresh/inactive tokens rejected; foreign origin 4403; missing origin configurable; token expiry → 4401 |
| `test_authorization.py` | own trip only; offered ≠ assigned driver; malformed ids; injected server event types; invalid JSON; oversized message → 4413; riders can't answer offers; unauthorized connections receive no pushes |
| `test_offers_ws.py` | offer to the selected driver only; WebSocket accept → authoritative result + rider `driver_assigned`; decline → rematch; expiry → `offer.closed` → late accept refused → rematch; someone else's offer; **concurrent accepts from two devices assign once**; **rolled-back assignment never pushed**; reply comes from the DB even if the action is a no-op; rider cancel → `offer.closed` |
| `test_trip_updates.py` | full milestone sequence; location pushes only for the assigned driver and only while active; `driver_approaching` exactly once; milestone endpoints validated (stranger 404, rider 403, skipped step 409, idempotent) |
| `test_lifecycle.py` | ping/pong keeps the socket alive and silence → 4408; disconnect cleans presence and groups; stale presence cleanup; **driver reconnect replays the pending offer**; **rider reconnect converges via snapshot**; server-driven join; **Redis channel layer delivers across independent layer clients (instances)** |

**Verified live** (Docker Compose; rider on `realtime-1`, driver on `realtime-2`):
- An anonymous handshake was refused (HTTP 403).
- The rider received the trip snapshot 32 ms after `POST /trips/`.
- The driver received `offer.created` 266 ms after the request, with the ETA
  labeled `fallback_straight_line`.
- The driver's WebSocket accept → `ok, ACCEPTED, DRIVER_ASSIGNED`.
- The rider on the other instance got `driver_assigned` (driver name and plate)
  **53 ms** later, and the REST API confirmed the same state.
- Two location pushes, then `driver_approaching` at 178 m.
- The driver's TCP connection was aborted (server logged code 1006 and cleaned
  up), and the driver reconnected to the *other* instance with a correct snapshot.
- `driver_arriving`, `trip_started` and `trip_completed` arrived in order, and
  ping/pong worked.
- No presence entries were left afterwards.

## Configuration

| Variable | Default |
|---|---|
| `CHANNEL_LAYER_BACKEND` | `redis` (`memory` for single-process tests) |
| `REALTIME_HEARTBEAT_INTERVAL_SECONDS` / `REALTIME_HEARTBEAT_TIMEOUT_SECONDS` | 25 / 60 |
| `REALTIME_MAX_MESSAGE_BYTES` | 4096 |
| `REALTIME_APPROACHING_RADIUS_M` | 300 |
| `REALTIME_ALLOWED_ORIGINS` | `CORS_ALLOWED_ORIGINS` |
| `REALTIME_ALLOW_MISSING_ORIGIN` | true |

## Known limitations

- **No guaranteed delivery.** WebSocket delivery is best effort by design; a
  client that misses a push converges on reconnect or `sync`. Push notifications
  (APNs/FCM) for backgrounded apps aren't implemented.
- **Location pushes aren't throttled per rider.** Every applied GPS fix of the
  assigned driver is forwarded (~1 per 4 s).
- **Disconnect doesn't change matching.** A disconnected driver can still be
  offered a ride and receives it on reconnect, or it expires. A
  "prefer connected drivers" ranking rule could use the presence registry later.
- **Staleness detection is coarse.** A crashed instance's group memberships live
  up to `group_expiry` (300 s). Pushes to those dead channels are simply dropped
  by the layer.
- **Approaching radius is straight-line.** The 300 m proximity check is
  straight-line, not road distance.
