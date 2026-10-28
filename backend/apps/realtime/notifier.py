"""WebSocket delivery layer: domain signals -> (after COMMIT) -> channel-layer groups.

Separation of concerns:
  * Business code (trips, matching, drivers, locations) only sends Django signals and
    knows nothing about WebSockets.
  * This module listens, waits for the transaction to COMMIT (transaction.on_commit),
    reloads nothing it does not need, and pushes ephemeral UI updates.
  * It does NOT read or write Kafka. Kafka (apps.events) carries durable facts for
    other services; WebSockets carry low-latency UI notifications to connected
    clients. Both are fed by the same signals, independently.
A failed push never affects the business transaction (it has already committed).
"""

from __future__ import annotations

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db import transaction
from django.dispatch import receiver

from apps.common.geo import haversine_km
from apps.common.metrics import metrics
from apps.locations.signals import driver_location_applied
from apps.matching.signals import ride_offer_accepted, ride_offer_closed, ride_offer_created
from apps.payments.signals import payment_status_changed
from apps.trips.signals import trip_requested, trip_status_changed

from . import presence
from .groups import driver_group, trip_group, user_group

logger = logging.getLogger("apps.realtime")

# Status -> rider-facing milestone name.
MILESTONES = {
    "SEARCHING": "searching",
    "DRIVER_ASSIGNED": "driver_assigned",
    "DRIVER_ARRIVING": "driver_arriving",
    "IN_PROGRESS": "trip_started",
    "COMPLETED": "trip_completed",
    "CANCELLED": "trip_cancelled",
}
LOCATION_VISIBLE = {"DRIVER_ASSIGNED", "DRIVER_ARRIVING", "IN_PROGRESS"}


def send(group: str, event: str, data: dict) -> None:
    """Push to a group now. Never raises."""
    try:
        async_to_sync(get_channel_layer().group_send)(
            group, {"type": "push", "event": event, "data": data}
        )
        metrics().increment("realtime.push", tags={"event": event})
    except Exception as exc:  # noqa: BLE001 - delivery must never break callers
        metrics().increment("realtime.push_failed", tags={"event": event})
        logger.warning("realtime.push_failed", extra={"event": event, "error": str(exc)[:200]})


def control(group: str, message: dict) -> None:
    try:
        async_to_sync(get_channel_layer().group_send)(group, message)
    except Exception as exc:  # noqa: BLE001
        logger.warning("realtime.control_failed", extra={"error": str(exc)[:200]})


def after_commit(fn) -> None:
    transaction.on_commit(fn, robust=True)


# --- offers -> the selected driver only ---------------------------------------------------------
@receiver(ride_offer_created, dispatch_uid="realtime.offer_created")
def on_offer_created(sender, offer, **kw):
    from .access import offer_payload

    data = offer_payload(offer)
    after_commit(lambda: send(driver_group(offer.driver_id), "offer.created", data))


@receiver(ride_offer_closed, dispatch_uid="realtime.offer_closed")
def on_offer_closed(sender, offer, **kw):
    data = {"offer_id": str(offer.id), "status": offer.status, "reason": offer.end_reason}
    after_commit(lambda: send(driver_group(offer.driver_id), "offer.closed", data))


@receiver(ride_offer_accepted, dispatch_uid="realtime.offer_accepted")
def on_offer_accepted(sender, offer, **kw):
    # Other devices of the same driver learn the offer is gone.
    data = {"offer_id": str(offer.id), "status": "ACCEPTED", "reason": "accepted"}
    after_commit(lambda: send(driver_group(offer.driver_id), "offer.closed", data))


# --- trips -> trip group (rider + assigned driver) -----------------------------------------------
@receiver(trip_requested, dispatch_uid="realtime.trip_requested")
def on_trip_requested(sender, trip, **kw):
    trip_id, rider_id = str(trip.pk), trip.rider_id
    after_commit(lambda: control(user_group(rider_id), {"type": "trip.join", "trip_id": trip_id}))


@receiver(trip_status_changed, dispatch_uid="realtime.trip_status")
def on_trip_status_changed(sender, trip, from_status, to_status, previous_driver_id=None, **kw):
    trip_id = str(trip.pk)
    driver_id = trip.driver_id
    driver_user_id = trip.driver.user_id if driver_id else None
    pickup = (float(trip.pickup_latitude), float(trip.pickup_longitude))
    released_user_id = None
    if previous_driver_id and previous_driver_id != driver_id:
        from apps.drivers.models import DriverProfile

        released_user_id = (
            DriverProfile.objects.filter(pk=previous_driver_id)
            .values_list("user_id", flat=True)
            .first()
        )

    def deliver():
        # A released driver leaves the trip BEFORE the status push goes out.
        if released_user_id:
            presence.clear_driver_trip(previous_driver_id)
            control(user_group(released_user_id),
                    {"type": "trip.leave", "trip_id": trip_id, "reason": "released"})  # fmt: skip
        # Server-driven subscription: the newly assigned driver's sockets join the trip.
        if to_status == "DRIVER_ASSIGNED" and driver_user_id:
            control(user_group(driver_user_id), {"type": "trip.join", "trip_id": trip_id})
        # Keep the driver->trip cache used for GPS fan-out in step with the trip.
        if driver_id and to_status in LOCATION_VISIBLE:
            presence.set_driver_trip(driver_id, {"trip_id": trip_id, "status": to_status,
                                                 "pickup": pickup})  # fmt: skip
        elif driver_id:
            presence.clear_driver_trip(driver_id)
        snapshot = _fresh_snapshot(trip_id)
        if snapshot is not None:
            send(trip_group(trip_id), "trip.status", {
                "trip_id": trip_id, "from_status": from_status, "status": to_status,
                "milestone": MILESTONES.get(to_status, to_status.lower()), "trip": snapshot,
            })  # fmt: skip

    after_commit(deliver)


def _fresh_snapshot(trip_id) -> dict | None:
    """Pushes carry COMMITTED state re-read from PostgreSQL, never the in-memory object."""
    from apps.trips.models import Trip

    from .access import trip_snapshot

    trip = Trip.objects.select_related("rider", "driver__user").filter(pk=trip_id).first()
    return trip_snapshot(trip) if trip else None


# --- payments -> the rider's own connections (live in-app status) -------------------------
@receiver(payment_status_changed, dispatch_uid="realtime.payment_status")
def on_payment_status_changed(sender, payment, from_status, to_status, **kw):
    data = {"payment_id": str(payment.pk), "trip_id": str(payment.trip_id), "status": to_status,
            "amount": str(payment.amount), "currency": payment.currency,
            "failure_code": payment.failure_code or None}  # fmt: skip
    rider_id = payment.rider_id
    after_commit(lambda: send(user_group(rider_id), "payment.status", data))


# --- driver GPS -> riders of that driver's active trip --------------------------------
@receiver(driver_location_applied, dispatch_uid="realtime.location")
def on_location_applied(sender, fix, **kw):
    info = presence.driver_trip(fix.driver_id)
    if not info or info.get("status") not in LOCATION_VISIBLE:
        return
    trip_id = info["trip_id"]
    data = {
        "trip_id": trip_id,
        "lat": float(fix.latitude),
        "lng": float(fix.longitude),
        "heading": fix.heading,
        "recorded_at": fix.recorded_at.isoformat(),
    }
    send(trip_group(trip_id), "trip.driver_location", data)
    if info["status"] in ("DRIVER_ASSIGNED", "DRIVER_ARRIVING"):
        meters = 1000 * haversine_km(float(fix.latitude), float(fix.longitude), *info["pickup"])
        if meters <= settings.REALTIME_APPROACHING_RADIUS_M and presence.once(
            f"approaching:{trip_id}", 6 * 3600
        ):
            send(trip_group(trip_id), "trip.driver_approaching",
                 {"trip_id": trip_id, "distance_m": round(meters)})  # fmt: skip
