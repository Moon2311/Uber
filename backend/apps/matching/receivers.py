"""Keep matching consistent with trip / driver changes made elsewhere, and record
offer activity in the trip audit log. All run synchronously inside the
transaction that made the change."""

from datetime import timedelta

from django.conf import settings
from django.dispatch import receiver
from django.utils import timezone

from apps.drivers.signals import driver_availability_changed
from apps.trips import audit
from apps.trips.models import ActorType, TripEventType
from apps.trips.signals import trip_requested, trip_status_changed
from apps.trips.state_machine import Actor

from .models import JobStatus, MatchingJob, OfferStatus
from .offers import cancel_pending_offers
from .orchestrator import create_job_for_trip
from .signals import ride_offer_accepted, ride_offer_closed, ride_offer_created
from .state import get_reservation_locks


@receiver(trip_requested, dispatch_uid="matching.create_job")
def on_trip_requested(sender, trip, **kw):
    create_job_for_trip(trip)


@receiver(trip_status_changed, dispatch_uid="matching.trip_status")
def on_trip_status_changed(sender, trip, from_status, to_status, **kw):
    if to_status == "SEARCHING" and from_status in ("DRIVER_ASSIGNED", "DRIVER_ARRIVING"):
        # Driver released the trip: resume matching with a fresh budget. Drivers
        # already offered this trip (including the one who released it) stay
        # excluded by RideOffer's (trip, driver) uniqueness.
        now = timezone.now()
        MatchingJob.objects.filter(trip_id=trip.pk).update(
            status=JobStatus.ACTIVE, next_run_at=now, rounds=0, offers_made=0,
            deadline=now + timedelta(seconds=settings.MATCHING_TIMEOUT_SECONDS),
            last_outcome="driver_released", lease_owner="", lease_until=None,
        )  # fmt: skip
    if to_status == "DRIVER_ASSIGNED":
        # accept_offer already does this; repeating it here makes "an ACTIVE job means
        # a matchable trip" hold for every assignment path (e.g. support tooling).
        MatchingJob.objects.filter(trip_id=trip.pk, status=JobStatus.ACTIVE).update(
            status=JobStatus.MATCHED, last_outcome="assigned", lease_owner="", lease_until=None
        )
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


# --- audit trail: offer rejection / expiry / withdrawal are trip events -----------------
_CLOSED_EVENT = {
    OfferStatus.DECLINED: (TripEventType.OFFER_DECLINED, ActorType.DRIVER),
    OfferStatus.EXPIRED: (TripEventType.OFFER_EXPIRED, ActorType.SYSTEM),
    OfferStatus.CANCELLED: (TripEventType.OFFER_CANCELLED, ActorType.SYSTEM),
}


@receiver(ride_offer_created, dispatch_uid="matching.audit_offer_created")
def audit_offer_created(sender, offer, **kw):
    audit.record(
        offer.trip_id, TripEventType.OFFER_SENT, actor=Actor.system(), driver_id=offer.driver_id,
        offer_id=offer.pk, occurred_at=offer.created_at,
        metadata={"rank": offer.rank, "pickup_eta_seconds": offer.pickup_eta_seconds,
                  "eta_source": offer.eta_source, "expires_at": offer.expires_at.isoformat()},
    )  # fmt: skip


@receiver(ride_offer_accepted, dispatch_uid="matching.audit_offer_accepted")
def audit_offer_accepted(sender, offer, **kw):
    audit.record(
        offer.trip_id, TripEventType.OFFER_ACCEPTED, actor=Actor(ActorType.DRIVER, offer.driver_id),
        driver_id=offer.driver_id, offer_id=offer.pk, occurred_at=offer.responded_at,
    )  # fmt: skip


@receiver(ride_offer_closed, dispatch_uid="matching.audit_offer_closed")
def audit_offer_closed(sender, offer, **kw):
    event_type, actor_type = _CLOSED_EVENT[offer.status]
    actor_id = offer.driver_id if actor_type == ActorType.DRIVER else None
    audit.record(
        offer.trip_id, event_type, actor=Actor(actor_type, actor_id), driver_id=offer.driver_id,
        offer_id=offer.pk, reason=offer.end_reason, occurred_at=offer.responded_at,
    )  # fmt: skip
