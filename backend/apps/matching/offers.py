"""Reservations and the offer lifecycle. Every transition is one PostgreSQL
transaction that locks rows in the global order

    Trip -> MatchingJob -> DriverProfile -> RideOffer

(see models.py) and re-validates everything under those locks. Unique constraints on RideOffer
and Trip are the last line of defence; an IntegrityError is a lost race, never a
double assignment.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.common.exceptions import ConflictError
from apps.common.metrics import metrics
from apps.drivers.models import AvailabilityStatus, DriverProfile, Vehicle
from apps.trips import state_machine
from apps.trips.models import Trip, TripStatus
from apps.trips.state_machine import Actor

from .models import JobStatus, MatchingJob, OfferStatus, RideOffer
from .ranking import RankedCandidate
from .signals import ride_offer_accepted, ride_offer_closed, ride_offer_created
from .state import ReservationLocks

logger = logging.getLogger("apps.matching")


class ReserveFailure(str, enum.Enum):
    LOCKED_ELSEWHERE = "locked_elsewhere"  # Redis lock held for another trip
    LEASE_LOST = "lease_lost"  # another worker owns the job now (fencing)
    TRIP_NOT_SEARCHING = "trip_not_searching"
    TRIP_HAS_OFFER = "trip_has_offer"
    DRIVER_UNAVAILABLE = "driver_unavailable"  # offline / no vehicle / incompatible
    DRIVER_BUSY = "driver_busy"  # on a trip
    DRIVER_RESERVED = "driver_reserved"  # pending offer for another trip
    ALREADY_OFFERED = "already_offered"
    CONSTRAINT_CONFLICT = "constraint_conflict"  # lost a race at the DB constraint


@dataclass(frozen=True)
class ReserveResult:
    offer: RideOffer | None = None
    failure: ReserveFailure | None = None


def reserve_and_offer(
    *,
    trip_id,
    candidate: RankedCandidate,
    lease_owner: str,
    locks: ReservationLocks,
    compatible_categories: set[str],
    now=None,
) -> ReserveResult:
    """Atomically reserve `candidate` for the trip by creating a PENDING offer."""
    driver_id = candidate.driver.driver_id
    ttl = settings.MATCHING_OFFER_TTL_SECONDS
    lock = locks.try_acquire(driver_id, str(trip_id), ttl + settings.MATCHING_LOCK_GRACE_SECONDS)
    if lock is False:
        return _fail(ReserveFailure.LOCKED_ELSEWHERE)
    # lock is True (held) or None (Redis unavailable -> PostgreSQL alone decides)
    result = ReserveResult(failure=ReserveFailure.CONSTRAINT_CONFLICT)
    try:
        result = _reserve_in_db(trip_id, candidate, lease_owner, compatible_categories, now)
    except IntegrityError:
        result = ReserveResult(failure=ReserveFailure.CONSTRAINT_CONFLICT)
    finally:
        if result.offer is None:
            locks.release(driver_id, str(trip_id))  # never leak a reservation
    if result.failure:
        metrics().increment("matching.assignment.failed", tags={"reason": result.failure.value})
    return result


def _fail(reason: ReserveFailure) -> ReserveResult:
    return ReserveResult(failure=reason)


def _reserve_in_db(trip_id, candidate, lease_owner, compatible, now) -> ReserveResult:
    driver_id = candidate.driver.driver_id
    with transaction.atomic():
        now = now or timezone.now()
        trip = Trip.objects.select_for_update().get(pk=trip_id)
        job = MatchingJob.objects.select_for_update().get(trip_id=trip_id)
        if (
            job.status != JobStatus.ACTIVE
            or job.lease_owner != lease_owner
            or (job.lease_until is None or job.lease_until <= now)
        ):
            return _fail(ReserveFailure.LEASE_LOST)
        if trip.status != TripStatus.SEARCHING:
            return _fail(ReserveFailure.TRIP_NOT_SEARCHING)
        if RideOffer.objects.filter(trip=trip, status=OfferStatus.PENDING).exists():
            return _fail(ReserveFailure.TRIP_HAS_OFFER)
        driver = DriverProfile.objects.select_for_update().get(pk=driver_id)
        category = (
            Vehicle.objects.filter(driver=driver, is_active=True)
            .values_list("category", flat=True)
            .first()
        )
        if driver.availability_status != AvailabilityStatus.ONLINE or category not in compatible:
            return _fail(ReserveFailure.DRIVER_UNAVAILABLE)
        if Trip.objects.active_for_driver(driver).exists():
            return _fail(ReserveFailure.DRIVER_BUSY)
        if RideOffer.objects.filter(driver=driver, status=OfferStatus.PENDING).exists():
            return _fail(ReserveFailure.DRIVER_RESERVED)
        if RideOffer.objects.filter(trip=trip, driver=driver).exists():
            return _fail(ReserveFailure.ALREADY_OFFERED)
        with transaction.atomic():  # savepoint: constraint violation -> clean failure
            offer = RideOffer.objects.create(
                trip=trip,
                driver=driver,
                expires_at=now + timedelta(seconds=settings.MATCHING_OFFER_TTL_SECONDS),
                rank=candidate.rank,
                score=candidate.score,
                pickup_eta_seconds=candidate.eta.seconds,
                eta_source=candidate.eta.source,
                eta_is_fallback=candidate.eta.is_fallback,
                straight_line_km=round(candidate.driver.raw.straight_line_km, 4),
                grid_distance=candidate.driver.raw.grid_distance,
            )
        job.offers_made += 1
        job.next_run_at = offer.expires_at
        job.last_outcome = "offered"
        job.save(update_fields=["offers_made", "next_run_at", "last_outcome", "updated_at"])
        ride_offer_created.send(sender=RideOffer, offer=offer)  # -> outbox (same txn)
    metrics().increment("matching.offer.created", tags={"eta_source": offer.eta_source})
    logger.info(
        "matching.offer.created",
        extra={"trip_id": str(trip_id), "offer_id": str(offer.id), "driver_id": driver_id,
               "rank": candidate.rank, "eta_s": candidate.eta.seconds,
               "eta_source": candidate.eta.source},
    )  # fmt: skip
    return ReserveResult(offer=offer)


# --- driver responses ------------------------------------------------------------------
@dataclass(frozen=True)
class ResponseResult:
    offer: RideOffer
    changed: bool  # False = idempotent replay of an earlier identical response


def accept_offer(*, offer_id, driver: DriverProfile, locks: ReservationLocks) -> ResponseResult:
    """Driver accepts. Idempotent: accepting an already-accepted offer returns it."""
    expired = False
    with transaction.atomic():
        now = timezone.now()
        offer = RideOffer.objects.filter(pk=offer_id, driver=driver).first()
        if offer is None:
            raise RideOffer.DoesNotExist
        trip = Trip.objects.select_for_update().get(pk=offer.trip_id)
        MatchingJob.objects.select_for_update().filter(trip_id=offer.trip_id).first()
        locked_driver = DriverProfile.objects.select_for_update().get(pk=driver.pk)
        offer = RideOffer.objects.select_for_update().get(pk=offer_id)
        if offer.status == OfferStatus.ACCEPTED:
            return ResponseResult(offer, changed=False)
        if offer.status != OfferStatus.PENDING:
            raise ConflictError(f"This offer is no longer available ({offer.status}).")
        if now >= offer.expires_at:
            _close(offer, OfferStatus.EXPIRED, "expired_on_accept", now)
            _wake_job(offer.trip_id, now)
            expired = True
        else:
            if trip.status != TripStatus.SEARCHING:
                _close(offer, OfferStatus.CANCELLED, "trip_not_searching", now)
                raise ConflictError("This trip is no longer available.")
            if locked_driver.availability_status != AvailabilityStatus.ONLINE:
                raise ConflictError("Go online to accept offers.")
            if Trip.objects.active_for_driver(locked_driver).exists():
                raise ConflictError("You already have an active trip.")
            offer.status = OfferStatus.ACCEPTED
            offer.responded_at = now
            offer.save(update_fields=["status", "responded_at", "updated_at"])
            # Trip.trip_one_active_per_driver is the final guard here.
            state_machine.transition(
                trip, TripStatus.DRIVER_ASSIGNED, actor=Actor.driver(locked_driver),
                driver=locked_driver, metadata={"offer_id": str(offer.pk)},
            )  # fmt: skip
            MatchingJob.objects.filter(trip_id=trip.pk).update(
                status=JobStatus.MATCHED, last_outcome="accepted", lease_owner="", lease_until=None
            )
            ride_offer_accepted.send(sender=RideOffer, offer=offer)
    locks.release(driver.pk, str(offer.trip_id))
    if expired:
        metrics().increment("matching.offer.expired", tags={"how": "on_accept"})
        raise ConflictError("This offer has expired.")
    metrics().increment("matching.offer.accepted")
    latency = (offer.responded_at - offer.created_at).total_seconds() * 1000
    metrics().timing("matching.offer.response_ms", latency)
    return ResponseResult(offer, changed=True)


def decline_offer(*, offer_id, driver: DriverProfile, locks: ReservationLocks) -> ResponseResult:
    with transaction.atomic():
        now = timezone.now()
        offer = RideOffer.objects.filter(pk=offer_id, driver=driver).first()
        if offer is None:
            raise RideOffer.DoesNotExist
        Trip.objects.select_for_update().get(pk=offer.trip_id)
        MatchingJob.objects.select_for_update().filter(trip_id=offer.trip_id).first()
        offer = RideOffer.objects.select_for_update().get(pk=offer_id)
        if offer.status == OfferStatus.DECLINED:
            return ResponseResult(offer, changed=False)
        if offer.status != OfferStatus.PENDING:
            raise ConflictError(f"This offer is no longer pending ({offer.status}).")
        _close(offer, OfferStatus.DECLINED, "declined", now)
        _wake_job(offer.trip_id, now)
    locks.release(driver.pk, str(offer.trip_id))
    metrics().increment("matching.offer.declined")
    return ResponseResult(offer, changed=True)


def expire_offers(*, locks: ReservationLocks, now=None, limit: int = 500) -> int:
    """Close PENDING offers past expiry and wake their jobs (runs in the worker)."""
    now = now or timezone.now()
    with transaction.atomic():
        due = list(
            RideOffer.objects.select_for_update(skip_locked=True)
            .filter(status=OfferStatus.PENDING, expires_at__lte=now)
            .order_by("expires_at")[:limit]
        )
        for offer in due:
            _close(offer, OfferStatus.EXPIRED, "timeout", now)
    # Separate statement after commit: keeps lock order (no job lock while holding offers).
    if due:
        MatchingJob.objects.filter(
            trip_id__in=[o.trip_id for o in due], status=JobStatus.ACTIVE
        ).update(next_run_at=now)
    for offer in due:
        locks.release(offer.driver_id, str(offer.trip_id))
    if due:
        metrics().increment("matching.offer.expired", len(due), tags={"how": "sweeper"})
    return len(due)


def cancel_pending_offers(*, trip_id=None, driver_id=None, reason: str, locks) -> list[RideOffer]:
    """Called from signal receivers inside the trip/driver transaction."""
    qs = RideOffer.objects.select_for_update().filter(status=OfferStatus.PENDING)
    qs = qs.filter(trip_id=trip_id) if trip_id else qs.filter(driver_id=driver_id)
    now = timezone.now()
    offers = list(qs)
    for offer in offers:
        _close(offer, OfferStatus.CANCELLED, reason, now)
    for offer in offers:
        locks.release(offer.driver_id, str(offer.trip_id))
    if offers:
        metrics().increment("matching.offer.cancelled", len(offers), tags={"reason": reason})
    return offers


def _close(offer, status, reason, now):
    offer.status = status
    offer.end_reason = reason
    offer.responded_at = now
    offer.save(update_fields=["status", "end_reason", "responded_at", "updated_at"])
    ride_offer_closed.send(sender=RideOffer, offer=offer)


def _wake_job(trip_id, now):
    MatchingJob.objects.filter(trip_id=trip_id, status=JobStatus.ACTIVE).update(next_run_at=now)
