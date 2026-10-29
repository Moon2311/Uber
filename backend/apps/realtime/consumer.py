"""The WebSocket endpoint ("RAMEN" in the reference diagram is only a conceptual
name; this is a plain Django Channels consumer).

Client -> server messages ({"type": ..., "id": <optional correlation id>}):
    ping                              -> pong (application heartbeat)
    sync                              -> resends trip snapshots + pending offers
    subscribe.trip   {trip_id}        -> authorized join of the trip channel + snapshot
    unsubscribe.trip {trip_id}
    offer.accept     {offer_id}       -> validated by PostgreSQL; reply offer.result
    offer.decline    {offer_id}
Server -> client:
    welcome, pong, error, subscribed, unsubscribed, offer.result,
    offer.created, offer.closed, trip.snapshot, trip.status, trip.driver_location,
    trip.driver_approaching

A client message is only a *request*. Results always come from the authoritative
PostgreSQL state (offer/trip rows after the transaction), never from what the
client claimed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings

from apps.common.metrics import metrics

from . import access, presence
from .auth import SUBPROTOCOL
from .groups import driver_group, trip_group, user_group

logger = logging.getLogger("apps.realtime")

CLOSE_UNAUTHENTICATED = 4401
CLOSE_HEARTBEAT_TIMEOUT = 4408
CLOSE_MESSAGE_TOO_LARGE = 4413
CLOSE_TOO_MANY_MESSAGES = 4429


class TokenBucket:
    """Inbound backpressure per connection: `rate` messages/second, bursts of `burst`."""

    def __init__(self, rate: float, burst: int, clock=time.monotonic):
        self.rate, self.capacity, self.clock = rate, float(burst), clock
        self.tokens, self.updated = float(burst), clock()

    def take(self) -> bool:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class RealtimeConsumer(AsyncJsonWebsocketConsumer):
    # --- lifecycle --------------------------------------------------------------------
    async def connect(self):
        self.user = self.scope.get("user")
        if not getattr(self.user, "is_authenticated", False):
            metrics().increment(
                "realtime.connect_rejected", tags={"reason": self.scope.get("auth_error")}
            )
            await self.close()  # before accept() -> handshake rejected (HTTP 403)
            return
        self.connection_id = uuid.uuid4().hex
        self.groups_joined: set[str] = set()
        self.trip_ids: set[str] = set()
        self.last_seen = time.monotonic()
        self.driver_id = getattr(getattr(self.user, "driver_profile", None), "pk", None)
        self.bucket = TokenBucket(settings.REALTIME_INBOUND_RATE_PER_SECOND,
                                  settings.REALTIME_INBOUND_BURST)  # fmt: skip
        self.violations = 0
        await self.accept(subprotocol=SUBPROTOCOL)
        metrics().gauge_add("realtime.connections", 1, tags={"role": self.user.role})

        await self._join(user_group(self.user.pk))
        if self.driver_id:
            await self._join(driver_group(self.driver_id))
        await database_sync_to_async(presence.touch)(self.user.pk, self.connection_id)
        self._watchdog = asyncio.ensure_future(self._heartbeat_watchdog())
        metrics().increment("realtime.connected", tags={"role": self.user.role})
        logger.info("realtime.connected", extra={"user_id": self.user.pk,
                                                 "connection_id": self.connection_id})  # fmt: skip
        await self.send_json({
            "type": "welcome",
            "connection_id": self.connection_id,
            "user": {"id": self.user.pk, "role": self.user.role},
            "heartbeat_interval_s": settings.REALTIME_HEARTBEAT_INTERVAL_SECONDS,
            "heartbeat_timeout_s": settings.REALTIME_HEARTBEAT_TIMEOUT_SECONDS,
            "server_time": time.time(),
        })  # fmt: skip
        await self._resync()

    async def disconnect(self, code):
        if not hasattr(self, "connection_id"):
            return
        if getattr(self, "_watchdog", None):
            self._watchdog.cancel()
        for group in list(self.groups_joined):
            await self.channel_layer.group_discard(group, self.channel_name)
        await database_sync_to_async(presence.remove)(self.user.pk, self.connection_id)
        metrics().gauge_add("realtime.connections", -1, tags={"role": self.user.role})
        metrics().increment("realtime.disconnected", tags={"code": str(code)})
        logger.info(
            "realtime.disconnected",
            extra={"user_id": self.user.pk, "code": code, "connection_id": self.connection_id},
        )

    async def _heartbeat_watchdog(self):
        interval = settings.REALTIME_HEARTBEAT_INTERVAL_SECONDS
        try:
            while True:
                await asyncio.sleep(min(interval, settings.REALTIME_HEARTBEAT_TIMEOUT_SECONDS) / 2)
                if time.monotonic() - self.last_seen > settings.REALTIME_HEARTBEAT_TIMEOUT_SECONDS:
                    metrics().increment("realtime.heartbeat_timeout")
                    await self.close(code=CLOSE_HEARTBEAT_TIMEOUT)
                    return
                exp = self.scope.get("token_exp")
                if exp is not None and time.time() >= exp:
                    # Reconnect with a refreshed token.
                    await self.send_json({"type": "error", "code": "token_expired"})
                    await self.close(code=CLOSE_UNAUTHENTICATED)
                    return
                # Refresh group memberships (channel layer group_expiry makes memberships
                # of crashed instances disappear) and presence.
                for group in list(self.groups_joined):
                    await self.channel_layer.group_add(group, self.channel_name)
                await database_sync_to_async(presence.touch)(self.user.pk, self.connection_id)
        except asyncio.CancelledError:
            pass

    # --- inbound ----------------------------------------------------------------------
    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        raw = text_data if text_data is not None else (bytes_data or b"").decode("utf-8", "replace")
        if len(raw.encode()) > settings.REALTIME_MAX_MESSAGE_BYTES:
            await self.close(code=CLOSE_MESSAGE_TOO_LARGE)
            return
        if not self.bucket.take():
            self.violations += 1
            metrics().increment("realtime.rate_limited")
            if self.violations >= settings.REALTIME_INBOUND_MAX_VIOLATIONS:
                await self.close(code=CLOSE_TOO_MANY_MESSAGES)
            else:
                await self._error(None, "rate_limited", "Too many messages; slow down.")
            return
        try:
            content = json.loads(raw)
        except ValueError:
            await self._error(None, "invalid_json", "Message must be JSON.")
            return
        if not isinstance(content, dict):
            await self._error(None, "invalid_message", "Message must be an object.")
            return
        await self.receive_json(content)

    async def receive_json(self, content, **kwargs):
        self.last_seen = time.monotonic()
        kind, msg_id = content.get("type"), content.get("id")
        handler = {
            "ping": self._on_ping,
            "sync": self._on_sync,
            "subscribe.trip": self._on_subscribe,
            "unsubscribe.trip": self._on_unsubscribe,
            "offer.accept": self._on_offer_response,
            "offer.decline": self._on_offer_response,
        }.get(kind)
        if handler is None:
            # Includes attempts to inject server events (e.g. "trip.status"): ignored.
            await self._error(msg_id, "unknown_type", f"Unsupported message type {kind!r}.")
            return
        metrics().increment("realtime.inbound", tags={"type": kind})
        await handler(content, msg_id)

    async def _on_ping(self, content, msg_id):
        await self.send_json({"type": "pong", "reply_to": msg_id, "server_time": time.time()})

    async def _on_sync(self, content, msg_id):
        await self._resync(reply_to=msg_id)

    async def _on_subscribe(self, content, msg_id):
        trip_id = _uuid(content.get("trip_id"))
        if trip_id is None:
            await self._error(msg_id, "invalid_trip_id", "trip_id must be a UUID.")
            return
        snapshot = await self._authorized_snapshot(trip_id)
        if snapshot is None:
            # Same answer for "does not exist" and "not yours": no existence leak.
            metrics().increment("realtime.subscribe_denied")
            await self._error(msg_id, "not_found", "Trip not found.")
            return
        await self._join(trip_group(trip_id))
        self.trip_ids.add(trip_id)
        await self.send_json({"type": "subscribed", "reply_to": msg_id, "trip_id": trip_id})
        await self.send_json({"type": "trip.snapshot", "data": snapshot})

    async def _on_unsubscribe(self, content, msg_id):
        trip_id = _uuid(content.get("trip_id"))
        if trip_id in self.trip_ids:
            await self.channel_layer.group_discard(trip_group(trip_id), self.channel_name)
            self.groups_joined.discard(trip_group(trip_id))
            self.trip_ids.discard(trip_id)
        await self.send_json({"type": "unsubscribed", "reply_to": msg_id, "trip_id": trip_id})

    async def _on_offer_response(self, content, msg_id):
        if not self.driver_id:
            await self._error(msg_id, "forbidden", "Only drivers can respond to offers.")
            return
        offer_id = _uuid(content.get("offer_id"))
        if offer_id is None:
            await self._error(msg_id, "invalid_offer_id", "offer_id must be a UUID.")
            return
        accept = content["type"] == "offer.accept"
        result = await database_sync_to_async(_respond_to_offer)(self.user, offer_id, accept)
        if result.get("trip_id") and result.get("assigned_to_me"):
            await self._join(trip_group(result["trip_id"]))
            self.trip_ids.add(result["trip_id"])
        await self.send_json({"type": "offer.result", "reply_to": msg_id, **result})

    # --- outbound (channel-layer events) ----------------------------------------------
    async def push(self, event):
        data = event["data"]
        if event["event"].startswith("trip.") and isinstance(data, dict):
            # A trip push already queued when this socket left the trip group
            # (trip.leave) must not leak to it.
            if data.get("trip_id") is not None and data["trip_id"] not in self.trip_ids:
                return
        await self.send_json({"type": event["event"], "data": data})

    async def trip_join(self, event):
        """Server-driven subscription (new trip / new assignment): re-authorize first."""
        trip_id = event["trip_id"]
        snapshot = await self._authorized_snapshot(trip_id)
        if snapshot is not None:
            await self._join(trip_group(trip_id))
            self.trip_ids.add(trip_id)
            await self.send_json({"type": "trip.snapshot", "data": snapshot})

    async def trip_leave(self, event):
        """Server-driven unsubscription: the driver was released from the trip, so
        their sockets must stop receiving its updates."""
        trip_id = event["trip_id"]
        if trip_id in self.trip_ids:
            await self.channel_layer.group_discard(trip_group(trip_id), self.channel_name)
            self.groups_joined.discard(trip_group(trip_id))
            self.trip_ids.discard(trip_id)
        await self.send_json(
            {"type": "trip.removed", "data": {"trip_id": trip_id, "reason": event.get("reason")}}
        )

    # --- helpers --------------------------------------------------------------------
    async def _join(self, group):
        await self.channel_layer.group_add(group, self.channel_name)
        self.groups_joined.add(group)

    async def _authorized_snapshot(self, trip_id):
        def load():
            trip = access.trip_for_user(self.user, trip_id)
            return access.trip_snapshot(trip) if trip else None

        return await database_sync_to_async(load)()

    async def _resync(self, reply_to=None):
        """State transfer on (re)connect: WebSocket is not a durable log, so a
        reconnecting client converges by receiving current state, not a replay."""

        def load():
            trips = {}
            for trip_id in access.active_trip_ids(self.user):
                trip = access.trip_for_user(self.user, trip_id)
                if trip:
                    trips[trip_id] = access.trip_snapshot(trip)
            return trips, access.pending_offers(self.user)

        trips, offers = await database_sync_to_async(load)()
        for trip_id, snapshot in trips.items():
            await self._join(trip_group(trip_id))
            self.trip_ids.add(trip_id)
            await self.send_json({"type": "trip.snapshot", "data": snapshot})
        for offer in offers:
            await self.send_json({"type": "offer.created", "data": offer, "replayed": True})
        await self.send_json({"type": "synced", "reply_to": reply_to,
                              "trips": sorted(trips), "pending_offers": len(offers)})  # fmt: skip

    async def _error(self, msg_id, code, message):
        await self.send_json({"type": "error", "reply_to": msg_id, "code": code,
                              "message": message})  # fmt: skip


def _uuid(value) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError):
        return None


def _respond_to_offer(user, offer_id, accept: bool) -> dict:
    """Run the SAME transactional code path as the HTTP API, then report the
    authoritative state read back from PostgreSQL."""
    from apps.common.exceptions import ConflictError
    from apps.matching.models import RideOffer
    from apps.matching.offers import accept_offer, decline_offer
    from apps.matching.state import get_reservation_locks
    from apps.trips.models import Trip

    action = accept_offer if accept else decline_offer
    outcome = {"ok": True, "error": None}
    try:
        action(offer_id=offer_id, driver=user.driver_profile, locks=get_reservation_locks())
    except RideOffer.DoesNotExist:
        return {"ok": False, "error": "not_found", "offer_id": offer_id}
    except ConflictError as exc:
        outcome = {"ok": False, "error": str(exc.detail)}
    offer = RideOffer.objects.get(pk=offer_id)
    trip = Trip.objects.get(pk=offer.trip_id)
    mine = trip.driver_id == user.driver_profile.pk and trip.status != "CANCELLED"
    result = {
        **outcome,
        "offer_id": offer_id,
        "offer_status": offer.status,
        "trip_id": str(trip.pk),
        "trip_status": trip.status,
        "assigned_to_me": mine,
    }
    if mine:
        result["trip"] = access.trip_snapshot(trip)
    metrics().increment(
        "realtime.offer_response", tags={"accept": str(accept).lower(), "ok": str(outcome["ok"])}
    )
    return result
