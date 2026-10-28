"""Keep matching consistent with trip / driver changes made elsewhere.
All run synchronously inside the transaction that made the change."""

from django.dispatch import receiver

from apps.drivers.signals import driver_availability_changed
from apps.trips.signals import trip_requested, trip_status_changed

from .models import JobStatus, MatchingJob
from .offers import cancel_pending_offers
from .orchestrator import create_job_for_trip
from .state import get_reservation_locks


@receiver(trip_requested, dispatch_uid="matching.create_job")
def on_trip_requested(sender, trip, **kw):
    create_job_for_trip(trip)


@receiver(trip_status_changed, dispatch_uid="matching.trip_status")
def on_trip_status_changed(sender, trip, from_status, to_status, **kw):
    if to_status == "CANCELLED":
        # Caller holds the Trip lock; lock order continues Job -> Offer.
        MatchingJob.objects.filter(trip_id=trip.pk, status=JobStatus.ACTIVE).update(
            status=JobStatus.CANCELLED, last_outcome="trip_cancelled", lease_owner="",
            lease_until=None,
        )  # fmt: skip
        cancel_pending_offers(
            trip_id=trip.pk, reason="trip_cancelled", locks=get_reservation_locks()
        )


@receiver(driver_availability_changed, dispatch_uid="matching.driver_offline")
def on_driver_availability_changed(sender, driver_id, status, changed_at, **kw):
    if status != "OFFLINE":
        return
    offers = cancel_pending_offers(
        driver_id=driver_id, reason="driver_offline", locks=get_reservation_locks()
    )
    if offers:
        # Caller holds the DriverProfile lock, which comes AFTER MatchingJob in the lock
        # order, so never wait for a job lock here: SKIP LOCKED. A skipped job is being
        # processed right now and its worker will see the cancelled offer anyway.
        due = MatchingJob.objects.select_for_update(skip_locked=True).filter(
            trip_id__in=[o.trip_id for o in offers], status=JobStatus.ACTIVE
        )
        MatchingJob.objects.filter(pk__in=list(due.values_list("pk", flat=True))).update(
            next_run_at=changed_at
        )
