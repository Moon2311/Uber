"""Payments follow the trip lifecycle; the trip lifecycle never reads payments.

Trip status and payment status are separate state machines in separate tables.
The trip domain only emits signals; these receivers run inside the trip's
transaction, so a trip and its payment record can never disagree about whether
the trip exists or was cancelled.
"""

from django.dispatch import receiver

from apps.trips.signals import trip_requested, trip_status_changed

from .services import cancel_payment_for_trip, create_pending_payment


@receiver(trip_requested, dispatch_uid="payments.trip_requested")
def on_trip_requested(sender, trip, **kw):
    create_pending_payment(trip=trip)


@receiver(trip_status_changed, dispatch_uid="payments.trip_status")
def on_trip_status_changed(sender, trip, to_status, **kw):
    if to_status == "CANCELLED":
        cancel_payment_for_trip(trip=trip)
    # COMPLETED deliberately does nothing yet: capture/settlement belongs to the
    # payments integration and reacts to TripCompleted, never the other way round.
