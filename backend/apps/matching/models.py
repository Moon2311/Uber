"""Matching state. PostgreSQL is the authority for who is offered / assigned what.

Lock order used everywhere (prevents deadlocks):
    Trip -> MatchingJob -> DriverProfile -> RideOffer
The Trip row is the outermost lock because code outside matching (e.g. a rider
cancelling) already locks the trip first and then reaches matching via signals.
"""

import uuid

from django.db import models

from apps.common.models import TimestampedModel


class OfferStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"  # the driver is RESERVED for this trip
    ACCEPTED = "ACCEPTED", "Accepted"
    DECLINED = "DECLINED", "Declined"
    EXPIRED = "EXPIRED", "Expired"
    CANCELLED = "CANCELLED", "Cancelled"  # trip cancelled / driver went offline


class RideOffer(TimestampedModel):
    """A time-limited offer of one trip to one driver.

    A PENDING offer *is* the driver's reservation. Database constraints are the
    final guard against conflicting assignment, whatever Redis or workers do:
      * at most one PENDING offer per driver  (a driver is reserved once)
      * at most one PENDING offer per trip    (a trip is offered to one driver)
      * at most one offer per (trip, driver)  (idempotency key; no re-offers)
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trip = models.ForeignKey("trips.Trip", on_delete=models.CASCADE, related_name="offers")
    driver = models.ForeignKey(
        "drivers.DriverProfile", on_delete=models.CASCADE, related_name="offers"
    )
    status = models.CharField(
        max_length=10, choices=OfferStatus.choices, default=OfferStatus.PENDING
    )
    expires_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=60, blank=True)
    # Ranking inputs, kept for audit / tuning.
    rank = models.PositiveSmallIntegerField()
    score = models.FloatField()
    pickup_eta_seconds = models.PositiveIntegerField()
    eta_source = models.CharField(max_length=40)
    eta_is_fallback = models.BooleanField()
    straight_line_km = models.FloatField()
    grid_distance = models.PositiveSmallIntegerField(null=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["expires_at"],
                name="offer_pending_expiry_idx",
                condition=models.Q(status="PENDING"),
            ),
            models.Index(fields=["driver", "status"], name="offer_driver_status_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["driver"],
                condition=models.Q(status="PENDING"),
                name="offer_one_pending_per_driver",
            ),
            models.UniqueConstraint(
                fields=["trip"],
                condition=models.Q(status="PENDING"),
                name="offer_one_pending_per_trip",
            ),
            models.UniqueConstraint(fields=["trip", "driver"], name="offer_once_per_trip_driver"),
            models.CheckConstraint(
                condition=models.Q(status__in=OfferStatus.values), name="offer_status_valid"
            ),
        ]

    def __str__(self):
        return f"Offer {self.id} trip={self.trip_id} driver={self.driver_id} [{self.status}]"


class JobStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    MATCHED = "MATCHED", "Matched"
    EXHAUSTED = "EXHAUSTED", "No driver found"
    CANCELLED = "CANCELLED", "Cancelled"


class MatchingJob(TimestampedModel):
    """Durable work item: "find a driver for this trip". Workers claim jobs with
    SELECT ... FOR UPDATE SKIP LOCKED and hold a time-limited lease; every write
    re-checks the lease (fencing), so a stalled worker cannot act on a job that
    another worker has taken over."""

    trip = models.OneToOneField(
        "trips.Trip", on_delete=models.CASCADE, primary_key=True, related_name="matching_job"
    )
    status = models.CharField(max_length=10, choices=JobStatus.choices, default=JobStatus.ACTIVE)
    next_run_at = models.DateTimeField()
    deadline = models.DateTimeField()
    rounds = models.PositiveIntegerField(default=0)
    offers_made = models.PositiveIntegerField(default=0)
    lease_owner = models.CharField(max_length=100, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    last_outcome = models.CharField(max_length=60, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["next_run_at"],
                name="matchjob_active_due_idx",
                condition=models.Q(status="ACTIVE"),
            )
        ]

    def __str__(self):
        return f"MatchingJob {self.trip_id} [{self.status}]"
