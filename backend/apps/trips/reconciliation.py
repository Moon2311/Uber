"""Detect (and optionally repair) trips whose dependants disagree with them.

Normal operation cannot produce these states: the trip, its audit events, payment
record, matching job, offers and outbox rows are all written in one PostgreSQL
transaction. This exists for what normal operation does not cover: manual SQL,
restored backups, bugs, rows created before a migration, and the relay giving up.

Repairs are concurrency-safe: each one locks the trip (outermost in the global
lock order Trip -> MatchingJob -> DriverProfile -> RideOffer), RE-CHECKS the
condition under the lock, and only then writes. Findings that cannot be repaired
without inventing history (audit gaps) are reported, never "fixed".

Run: manage.py reconcile_trips [--fix] [--hours 48]
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, F, Max, OuterRef, Q
from django.utils import timezone

from apps.common.metrics import metrics

from .models import Trip, TripEvent, TripEventType, TripStatus

logger = logging.getLogger("apps.trips")

MATCHABLE = [TripStatus.REQUESTED, TripStatus.SEARCHING]
# Trips sitting in a driver-bound status this long need a human (report only).
STUCK_AFTER = {
    TripStatus.DRIVER_ASSIGNED: timedelta(hours=1),
    TripStatus.DRIVER_ARRIVING: timedelta(hours=1),
    TripStatus.IN_PROGRESS: timedelta(hours=12),
}


@dataclass
class Report:
    found: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    repaired: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def add(self, check: str, trip_id) -> None:
        self.found[check].append(str(trip_id))

    @property
    def clean(self) -> bool:
        return not any(self.found.values())

    def summary(self) -> dict:
        return {
            "found": {k: len(v) for k, v in self.found.items() if v},
            "repaired": {k: len(v) for k, v in self.repaired.items() if v},
        }


def reconcile(*, fix: bool = False, hours: float | None = 48, now=None) -> Report:
    from apps.events.models import OutboxEvent, OutboxStatus
    from apps.matching.models import JobStatus, MatchingJob, OfferStatus, RideOffer
    from apps.payments.models import PaymentRecord, PaymentStatus

    now = now or timezone.now()
    report = Report()
    trips = Trip.objects.all()
    if hours is not None:
        trips = trips.filter(updated_at__gte=now - timedelta(hours=hours))

    # 1. Audit chain: the latest STATUS_CHANGED must match the row. A mismatch means
    #    something wrote trips_trip without state_machine.transition. Report only.
    latest = TripEvent.objects.filter(
        trip=OuterRef("pk"), event_type=TripEventType.STATUS_CHANGED
    ).order_by("-state_version")
    for trip_id in (
        trips.annotate(
            last_version=Max(
                "events__state_version", filter=Q(events__event_type="STATUS_CHANGED")
            ),
            last_status=latest.values("to_status")[:1],
        )
        .filter(
            Q(last_version__isnull=True)
            | ~Q(last_version=F("state_version"))
            | ~Q(last_status=F("status"))
        )
        .values_list("pk", flat=True)
    ):
        report.add("audit_chain_mismatch", trip_id)

    # 2. Matchable trip with no active matching job: it would wait forever.
    active_job = MatchingJob.objects.filter(trip=OuterRef("pk"), status=JobStatus.ACTIVE)
    for trip_id in (
        trips.filter(status__in=MATCHABLE)
        .annotate(has_job=Exists(active_job))
        .filter(has_job=False)
        .values_list("pk", flat=True)
    ):
        report.add("matchable_without_active_job", trip_id)
        if fix and _repair_job(trip_id, now):
            report.repaired["matchable_without_active_job"].append(str(trip_id))

    # 3. Active job for a trip that no longer needs matching: wasted worker rounds.
    for trip_id in (
        MatchingJob.objects.filter(status=JobStatus.ACTIVE)
        .exclude(trip__status__in=MATCHABLE)
        .values_list("trip_id", flat=True)
    ):
        report.add("active_job_for_closed_trip", trip_id)
        if fix and _close_job(trip_id):
            report.repaired["active_job_for_closed_trip"].append(str(trip_id))

    # 4. PENDING offer on a trip that is not SEARCHING: a driver is reserved for nothing.
    for trip_id in set(
        RideOffer.objects.filter(status=OfferStatus.PENDING)
        .exclude(trip__status=TripStatus.SEARCHING)
        .values_list("trip_id", flat=True)
    ):
        report.add("pending_offer_for_non_searching_trip", trip_id)
        if fix and _cancel_offers(trip_id):
            report.repaired["pending_offer_for_non_searching_trip"].append(str(trip_id))

    # 5. Payments follow trips: one record per trip; cancelled trip -> no live charge.
    for trip_id in (
        trips.annotate(has_payment=Exists(PaymentRecord.objects.filter(trip=OuterRef("pk"))))
        .filter(has_payment=False)
        .values_list("pk", flat=True)
    ):
        report.add("missing_payment_record", trip_id)
        if fix and _create_payment(trip_id):
            report.repaired["missing_payment_record"].append(str(trip_id))
    for trip_id in PaymentRecord.objects.filter(
        status=PaymentStatus.PENDING, trip__status=TripStatus.CANCELLED, trip__in=trips
    ).values_list("trip_id", flat=True):
        report.add("payment_live_for_cancelled_trip", trip_id)
        if fix and _cancel_payment(trip_id):
            report.repaired["payment_live_for_cancelled_trip"].append(str(trip_id))

    # 6. Report-only: trips stuck in a driver-bound status, audit rows without a
    #    trip, and events the relay gave up on (Kafka consumers never saw them).
    for status, age in STUCK_AFTER.items():
        for trip_id in Trip.objects.filter(status=status, updated_at__lt=now - age).values_list(
            "pk", flat=True
        ):
            report.add(f"stuck_{status.lower()}", trip_id)
    orphans = TripEvent.objects.exclude(Exists(Trip.objects.filter(pk=OuterRef("trip_id"))))
    for trip_id in set(orphans.values_list("trip_id", flat=True)):
        report.add("orphan_trip_events", trip_id)
    for key in OutboxEvent.objects.filter(
        status=OutboxStatus.FAILED, topic="trip.events.v1"
    ).values_list("key", flat=True):
        report.add("outbox_gave_up", key)

    for check, ids in report.found.items():
        if ids:
            metrics().gauge("trips.reconcile.found", len(ids), tags={"check": check})
    logger.info("trips.reconcile", extra={"fix": fix, **report.summary()})
    return report


# --- repairs: lock trip, re-check, write ------------------------------------------------
def _repair_job(trip_id, now) -> bool:
    from django.conf import settings

    from apps.matching.models import JobStatus, MatchingJob

    with transaction.atomic():
        trip = Trip.objects.select_for_update().get(pk=trip_id)
        if trip.status not in MATCHABLE:
            return False
        job, created = MatchingJob.objects.select_for_update().get_or_create(
            trip=trip,
            defaults={"next_run_at": now,
                      "deadline": now + timedelta(seconds=settings.MATCHING_TIMEOUT_SECONDS)},
        )  # fmt: skip
        if not created:
            if job.status == JobStatus.ACTIVE:
                return False
            job.status = JobStatus.ACTIVE
            job.next_run_at = now
            job.deadline = max(job.deadline, now + timedelta(seconds=30))
            job.lease_owner, job.lease_until = "", None
            job.last_outcome = "reconciled"
            job.save()
    return True


def _close_job(trip_id) -> bool:
    from apps.matching.models import JobStatus, MatchingJob

    with transaction.atomic():
        trip = Trip.objects.select_for_update().get(pk=trip_id)
        if trip.status in MATCHABLE:
            return False
        status = JobStatus.CANCELLED if trip.status == TripStatus.CANCELLED else JobStatus.MATCHED
        return bool(
            MatchingJob.objects.filter(trip_id=trip_id, status=JobStatus.ACTIVE).update(
                status=status, last_outcome="reconciled", lease_owner="", lease_until=None
            )
        )


def _cancel_offers(trip_id) -> bool:
    from apps.matching.offers import cancel_pending_offers
    from apps.matching.state import get_reservation_locks

    with transaction.atomic():
        trip = Trip.objects.select_for_update().get(pk=trip_id)
        if trip.status == TripStatus.SEARCHING:
            return False
        return bool(
            cancel_pending_offers(
                trip_id=trip_id, reason="reconciled", locks=get_reservation_locks()
            )
        )


def _create_payment(trip_id) -> bool:
    from apps.payments.models import PaymentRecord, PaymentStatus

    with transaction.atomic():
        trip = Trip.objects.select_for_update().get(pk=trip_id)
        if PaymentRecord.objects.filter(trip=trip).exists():
            return False
        PaymentRecord.objects.create(
            trip=trip, rider_id=trip.rider_id, amount=trip.estimated_fare, currency=trip.currency,
            status=PaymentStatus.CANCELLED if trip.status == TripStatus.CANCELLED
            else PaymentStatus.PENDING,
        )  # fmt: skip
    return True


def _cancel_payment(trip_id) -> bool:
    from apps.payments.services import cancel_payment_for_trip

    with transaction.atomic():
        trip = Trip.objects.select_for_update().get(pk=trip_id)
        if trip.status != TripStatus.CANCELLED:
            return False
        return bool(cancel_payment_for_trip(trip=trip))
