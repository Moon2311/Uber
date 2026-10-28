"""Translate domain signals into versioned events.

* Trip and availability events -> transactional outbox (same DB transaction).
* Location fixes -> sampled, best-effort stream (no outbox, no DB write).
"""

import logging

from django.dispatch import receiver

from apps.drivers.signals import driver_availability_changed
from apps.locations.signals import driver_location_applied
from apps.trips.signals import trip_requested, trip_status_changed

from . import builders, outbox

logger = logging.getLogger("apps.events")


@receiver(driver_availability_changed, dispatch_uid="events.availability")
def on_availability_changed(sender, signal=None, **kw):
    outbox.enqueue(builders.driver_availability_changed(**kw))


@receiver(trip_requested, dispatch_uid="events.trip_requested")
def on_trip_requested(sender, trip, **kw):
    outbox.enqueue(builders.ride_requested(trip))


@receiver(trip_status_changed, dispatch_uid="events.trip_status")
def on_trip_status_changed(sender, trip, from_status, to_status, reason=None, **kw):
    outbox.enqueue(builders.trip_status_changed(trip, from_status, to_status, reason))
    if to_status == "COMPLETED":
        outbox.enqueue(builders.trip_completed(trip))


@receiver(driver_location_applied, dispatch_uid="events.location")
def on_location_applied(sender, fix, **kw):
    from .location_stream import publish_sampled_location

    try:
        publish_sampled_location(fix)
    except Exception:  # noqa: BLE001 - never break a GPS update for analytics
        logger.exception("events.location.publish_error")
