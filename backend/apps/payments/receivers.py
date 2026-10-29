"""Payments follow the trip lifecycle; the trip lifecycle never reads payments.

Trip status and payment status are separate state machines in separate tables.
These receivers run inside the trip's transaction, so a trip and its payment
record can never disagree about whether the trip exists, was cancelled or ended.
Talking to the provider never happens here: card charges are picked up by the
payments worker after commit (PostgreSQL is the queue; no dual write).
"""

from django.dispatch import receiver

from apps.trips.signals import trip_requested, trip_status_changed

from .services import cancel_payment_for_trip, create_pending_payment, on_trip_completed


@receiver(trip_requested, dispatch_uid="payments.trip_requested")
def on_trip_requested(sender, trip, payment_method_id=None, **kw):
    create_pending_payment(trip=trip, payment_method_id=payment_method_id)


@receiver(trip_status_changed, dispatch_uid="payments.trip_status")
def on_trip_status_changed(sender, trip, to_status, **kw):
    if to_status == "CANCELLED":
        cancel_payment_for_trip(trip=trip)
    elif to_status == "COMPLETED":
        on_trip_completed(trip=trip)
