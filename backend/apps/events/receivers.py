"""Translate domain signals into versioned events.

* Trip and availability events -> transactional outbox (same DB transaction).
* Location fixes -> sampled, best-effort stream (no outbox, no DB write).
"""

import logging

from django.dispatch import receiver

from apps.drivers.signals import driver_availability_changed
from apps.locations.signals import driver_location_applied
from apps.matching.signals import ride_offer_accepted, ride_offer_closed, ride_offer_created
from apps.trips.signals import trip_requested, trip_status_changed

from . import builders, outbox

logger = logging.getLogger("apps.events")


@receiver(driver_availability_changed, dispatch_uid="events.availability")
def on_availability_changed(sender, signal=None, **kw):
    outbox.enqueue(builders.driver_availability_changed(**kw))


@receiver(trip_requested, dispatch_uid="events.trip_requested")
def on_trip_requested(sender, trip, event_id=None, **kw):
    outbox.enqueue(builders.ride_requested(trip, event_id=event_id))


@receiver(trip_status_changed, dispatch_uid="events.trip_status")
def on_trip_status_changed(sender, trip, from_status, to_status, reason=None, event_id=None,
                           **kw):  # fmt: skip
    outbox.enqueue(
        builders.trip_status_changed(trip, from_status, to_status, reason, event_id=event_id)
    )
    if to_status == "COMPLETED":
        outbox.enqueue(builders.trip_completed(trip))


@receiver(driver_location_applied, dispatch_uid="events.location")
def on_location_applied(sender, fix, **kw):
    from .location_stream import publish_sampled_location

    try:
        publish_sampled_location(fix)
    except Exception:  # noqa: BLE001 - never break a GPS update for analytics
        logger.exception("events.location.publish_error")


@receiver(ride_offer_created, dispatch_uid="events.offer_created")
def on_offer_created(sender, offer, **kw):
    outbox.enqueue(
        builders.ride_offer_created(
            trip_id=offer.trip_id,
            offer_id=offer.id,
            driver_id=offer.driver_id,
            expires_at=offer.expires_at,
            created_at=offer.created_at,
            straight_line_km=offer.straight_line_km,
            grid_distance=offer.grid_distance,
        )  # fmt: skip
    )


@receiver(ride_offer_accepted, dispatch_uid="events.offer_accepted")
def on_offer_accepted(sender, offer, **kw):
    outbox.enqueue(
        builders.ride_accepted(
            trip_id=offer.trip_id,
            offer_id=offer.id,
            driver_id=offer.driver_id,
            accepted_at=offer.responded_at,
        )  # fmt: skip
    )


@receiver(ride_offer_closed, dispatch_uid="events.offer_closed")
def on_offer_closed(sender, offer, **kw):
    outbox.enqueue(
        builders.ride_offer_closed(
            trip_id=offer.trip_id,
            offer_id=offer.id,
            driver_id=offer.driver_id,
            status=offer.status,
            reason=offer.end_reason,
            closed_at=offer.responded_at,
        )  # fmt: skip
    )
