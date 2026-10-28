"""Trip-matching orchestration (the only module that knows the whole workflow).

    claim job (lease)  ->  K-Ring/Batch search  ->  Driver State filter
    ->  shortlist  ->  pickup ETAs  ->  rank  ->  reserve best (atomic) / next ...
    ->  PENDING offer with expiry  ->  [accept | decline | expire -> next round]

Bounded everywhere: rings/radius per search, ETA shortlist size, reservation
attempts per round, offers per trip, rounds back off, and a hard deadline after
which the trip is cancelled with reason "no_driver_available".
"""

from __future__ import annotations

import enum
import logging
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.common.metrics import metrics
from apps.trips import state_machine
from apps.trips.models import Trip, TripStatus

from .discovery import KRingSearch
from .eta import ResilientEtaService, get_eta_service
from .models import JobStatus, MatchingJob, OfferStatus, RideOffer
from .offers import reserve_and_offer
from .ranking import RankingPolicy, WeightedEtaRanking
from .state import DriverStateStore

logger = logging.getLogger("apps.matching")


class RoundOutcome(str, enum.Enum):
    OFFERED = "offered"
    WAITING = "waiting_for_driver"  # an offer is still pending
    NO_CANDIDATES = "no_candidates"
    RESERVATION_FAILED = "reservation_failed"
    EXHAUSTED = "exhausted"  # deadline / max offers -> trip cancelled
    CLOSED = "closed"  # trip no longer needs matching
    LEASE_LOST = "lease_lost"


@dataclass
class RoundReport:
    trip_id: str
    outcome: RoundOutcome
    raw_candidates: int = 0
    eligible: int = 0
    rings_searched: int = 0
    radius_km: float = 0.0
    rejected: dict = field(default_factory=dict)
    offer_id: str | None = None
    attempts: int = 0


def backoff(rounds: int) -> float:
    return min(
        settings.MATCHING_RETRY_MAX_SECONDS,
        settings.MATCHING_RETRY_BASE_SECONDS * 2 ** max(0, rounds - 1),
    )


class MatchingOrchestrator:
    def __init__(
        self,
        *,
        search: KRingSearch | None = None,
        state: DriverStateStore | None = None,
        eta: ResilientEtaService | None = None,
        ranking: RankingPolicy | None = None,
        owner: str | None = None,
        clock=timezone.now,
    ):
        self.search = search or KRingSearch()
        self.state = state or DriverStateStore()
        self.eta = eta or get_eta_service()
        self.ranking = ranking or WeightedEtaRanking()
        self.owner = owner or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self.clock = clock

    # --- job queue ----------------------------------------------------------------
    def claim(self, limit: int) -> list[str]:
        now = self.clock()
        with transaction.atomic():
            jobs = list(
                MatchingJob.objects.select_for_update(skip_locked=True)
                .filter(status=JobStatus.ACTIVE, next_run_at__lte=now)
                .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
                .order_by("next_run_at")[:limit]
            )
            for job in jobs:
                job.lease_owner = self.owner
                job.lease_until = now + timedelta(seconds=settings.MATCHING_LEASE_SECONDS)
                job.save(update_fields=["lease_owner", "lease_until", "updated_at"])
        return [str(j.trip_id) for j in jobs]

    def run_once(self, limit: int = 20) -> list[RoundReport]:
        reports = []
        for trip_id in self.claim(limit):
            try:
                reports.append(self.process(trip_id))
            except Exception:
                logger.exception("matching.round.error", extra={"trip_id": trip_id})
                metrics().increment("matching.round.error")
                self._finish(trip_id, next_run_in=settings.MATCHING_RETRY_BASE_SECONDS,
                             outcome="error")  # fmt: skip
        return reports

    # --- one matching round ---------------------------------------------------------
    def process(self, trip_id: str) -> RoundReport:
        start = time.perf_counter()
        report = self._round(trip_id)
        elapsed = (time.perf_counter() - start) * 1000
        tags = {"outcome": report.outcome.value}
        metrics().timing("matching.round.latency_ms", elapsed, tags=tags)
        metrics().increment("matching.round", tags=tags)
        metrics().gauge("matching.candidates.raw", report.raw_candidates)
        metrics().gauge("matching.candidates.eligible", report.eligible)
        metrics().gauge("matching.search.rings", report.rings_searched)
        metrics().gauge("matching.search.radius_km", round(report.radius_km, 3))
        logger.info(
            "matching.round",
            extra={"trip_id": trip_id, "outcome": report.outcome.value,
                   "raw": report.raw_candidates, "eligible": report.eligible,
                   "rings": report.rings_searched, "radius_km": round(report.radius_km, 3),
                   "rejected": report.rejected, "attempts": report.attempts,
                   "latency_ms": round(elapsed, 1)},
        )  # fmt: skip
        return report

    def _round(self, trip_id: str) -> RoundReport:
        now = self.clock()
        job = MatchingJob.objects.get(trip_id=trip_id)
        trip = Trip.objects.get(pk=trip_id)
        if job.lease_owner != self.owner:
            return RoundReport(trip_id, RoundOutcome.LEASE_LOST)
        if trip.status not in (TripStatus.REQUESTED, TripStatus.SEARCHING):
            self._close_job(trip_id, JobStatus.CANCELLED, "trip_closed")
            return RoundReport(trip_id, RoundOutcome.CLOSED)
        pending = RideOffer.objects.filter(trip_id=trip_id, status=OfferStatus.PENDING).first()
        if pending is not None:
            self._finish(trip_id, at=pending.expires_at, outcome="waiting")
            return RoundReport(trip_id, RoundOutcome.WAITING)
        if now >= job.deadline or job.offers_made >= settings.MATCHING_MAX_OFFERS_PER_TRIP:
            self._exhaust(trip_id)
            return RoundReport(trip_id, RoundOutcome.EXHAUSTED)
        if trip.status == TripStatus.REQUESTED:
            if not self._start_searching(trip_id):
                return RoundReport(trip_id, RoundOutcome.LEASE_LOST)

        report = RoundReport(trip_id, RoundOutcome.NO_CANDIDATES)
        ranked = self._candidates(trip, report)
        compatible = set(settings.MATCHING_CATEGORY_COMPATIBILITY.get(trip.ride_category, []))
        for candidate in ranked[: settings.MATCHING_MAX_RESERVE_ATTEMPTS]:
            report.attempts += 1
            result = reserve_and_offer(
                trip_id=trip_id, candidate=candidate, lease_owner=self.owner,
                locks=self.state.locks, compatible_categories=compatible,
            )  # fmt: skip
            if result.offer is not None:
                report.outcome = RoundOutcome.OFFERED
                report.offer_id = str(result.offer.id)
                self._release_lease(trip_id)
                return report
            if result.failure.value in ("lease_lost", "trip_not_searching", "trip_has_offer"):
                report.outcome = RoundOutcome.LEASE_LOST
                return report  # someone else owns the next step; do not keep trying
            report.outcome = RoundOutcome.RESERVATION_FAILED

        job.refresh_from_db(fields=["rounds"])
        rounds = job.rounds + 1
        MatchingJob.objects.filter(trip_id=trip_id, lease_owner=self.owner).update(rounds=rounds)
        self._finish(trip_id, next_run_in=backoff(rounds), outcome=report.outcome.value)
        return report

    def _candidates(self, trip: Trip, report: RoundReport):
        lat, lng = float(trip.pickup_latitude), float(trip.pickup_longitude)
        exclude = set(RideOffer.objects.filter(trip=trip).values_list("driver_id", flat=True))
        target = settings.MATCHING_TARGET_CANDIDATES
        eligible, rejected = [], {}
        enough_at = None
        for ring in self.search.rings(
            lat, lng, max_k=settings.MATCHING_MAX_K, max_radius_km=settings.MATCHING_MAX_RADIUS_KM
        ):
            report.rings_searched += 1
            report.raw_candidates += len(ring.candidates)
            kept, why = self.state.filter_eligible(
                ring.candidates, ride_category=trip.ride_category, trip_id=str(trip.pk),
                exclude=exclude,
            )  # fmt: skip
            eligible += kept
            for k, v in why.items():
                rejected[k] = rejected.get(k, 0) + v
            report.radius_km = max([report.radius_km] + [c.raw.straight_line_km for c in kept])
            # Insufficient -> expand. Once sufficient, search ONE more ring: a driver
            # just across the boundary can be closer than ones already found.
            if enough_at is None and len(eligible) >= target and ring.k >= 1:
                enough_at = ring.k
            elif enough_at is not None:
                break
        report.eligible = len(eligible)
        report.rejected = rejected
        if not eligible:
            return []
        # Bound routing calls: only the nearest few (straight line) get a real ETA.
        shortlist = sorted(eligible, key=lambda d: (d.raw.straight_line_km, d.driver_id))[
            : settings.MATCHING_ETA_SHORTLIST
        ]
        etas = self.eta.estimate(
            [(float(d.raw.fix.latitude), float(d.raw.fix.longitude)) for d in shortlist],
            (lat, lng),
        )
        return self.ranking.rank(list(zip(shortlist, etas, strict=True)))

    # --- state changes (all fenced by the lease) ------------------------------------
    def _owned_job(self, trip_id):
        job = MatchingJob.objects.select_for_update().get(trip_id=trip_id)
        now = self.clock()
        if job.lease_owner != self.owner or not job.lease_until or job.lease_until <= now:
            return None
        return job

    def _start_searching(self, trip_id) -> bool:
        with transaction.atomic():
            trip = Trip.objects.select_for_update().get(pk=trip_id)  # lock order: trip first
            if self._owned_job(trip_id) is None:
                return False
            if trip.status == TripStatus.REQUESTED:
                state_machine.transition(trip, TripStatus.SEARCHING)
            return trip.status == TripStatus.SEARCHING

    def _exhaust(self, trip_id):
        with transaction.atomic():
            trip = Trip.objects.select_for_update().get(pk=trip_id)  # lock order: trip first
            if self._owned_job(trip_id) is None:
                return
            if trip.status in (TripStatus.REQUESTED, TripStatus.SEARCHING):
                # Signal receivers cancel offers, payment record and close the job.
                state_machine.transition(
                    trip, TripStatus.CANCELLED, cancellation_reason="no_driver_available"
                )
                from apps.payments.services import cancel_payment_for_trip

                cancel_payment_for_trip(trip=trip)
            MatchingJob.objects.filter(trip_id=trip_id).update(
                status=JobStatus.EXHAUSTED, last_outcome="exhausted", lease_owner="",
                lease_until=None,
            )  # fmt: skip
        metrics().increment("matching.exhausted")
        metrics().increment("matching.assignment.failed", tags={"reason": "exhausted"})

    def _close_job(self, trip_id, status, outcome):
        MatchingJob.objects.filter(trip_id=trip_id).update(
            status=status, last_outcome=outcome, lease_owner="", lease_until=None
        )

    def _finish(self, trip_id, *, next_run_in=None, at=None, outcome=""):
        at = at or self.clock() + timedelta(seconds=next_run_in or 0)
        MatchingJob.objects.filter(trip_id=trip_id, lease_owner=self.owner).update(
            next_run_at=at, last_outcome=outcome, lease_owner="", lease_until=None
        )

    def _release_lease(self, trip_id):
        MatchingJob.objects.filter(trip_id=trip_id, lease_owner=self.owner).update(
            lease_owner="", lease_until=None
        )


def create_job_for_trip(trip) -> MatchingJob:
    now = timezone.now()
    job, _ = MatchingJob.objects.get_or_create(
        trip=trip,
        defaults={
            "next_run_at": now,
            "deadline": now + timedelta(seconds=settings.MATCHING_TIMEOUT_SECONDS),
        },
    )
    return job
