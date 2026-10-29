import uuid

from django.conf import settings
from django.db import models

from apps.common.geo import COORDINATE_FIELD_KWARGS, LATITUDE_VALIDATORS, LONGITUDE_VALIDATORS
from apps.common.models import TimestampedModel
from apps.drivers.models import RideCategory


class TripStatus(models.TextChoices):
    REQUESTED = "REQUESTED", "Requested"
    SEARCHING = "SEARCHING", "Searching"
    DRIVER_ASSIGNED = "DRIVER_ASSIGNED", "Driver assigned"
    DRIVER_ARRIVING = "DRIVER_ARRIVING", "Driver arriving"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    COMPLETED = "COMPLETED", "Completed"
    CANCELLED = "CANCELLED", "Cancelled"


ACTIVE_STATUSES = [
    TripStatus.REQUESTED,
    TripStatus.SEARCHING,
    TripStatus.DRIVER_ASSIGNED,
    TripStatus.DRIVER_ARRIVING,
    TripStatus.IN_PROGRESS,
]
DRIVER_BOUND_STATUSES = [
    TripStatus.DRIVER_ASSIGNED,
    TripStatus.DRIVER_ARRIVING,
    TripStatus.IN_PROGRESS,
    TripStatus.COMPLETED,
]
DRIVER_ACTIVE_STATUSES = [
    TripStatus.DRIVER_ASSIGNED,
    TripStatus.DRIVER_ARRIVING,
    TripStatus.IN_PROGRESS,
]
TERMINAL_STATUSES = [TripStatus.COMPLETED, TripStatus.CANCELLED]


class ActorType(models.TextChoices):
    """Who caused a trip change. Part of the transition rules, not just audit."""

    RIDER = "RIDER", "Rider"
    DRIVER = "DRIVER", "Driver"
    SYSTEM = "SYSTEM", "System"  # matching worker, timeouts, reconciliation
    ADMIN = "ADMIN", "Admin"  # staff / support tooling


class TripQuerySet(models.QuerySet):
    def active(self):
        return self.filter(status__in=ACTIVE_STATUSES)

    def active_for_driver(self, driver):
        return self.filter(driver=driver, status__in=DRIVER_ACTIVE_STATUSES)

    def visible_to(self, user):
        """Ownership scoping: riders see their trips, drivers see trips assigned to them."""
        if user.is_staff:
            return self
        if user.is_driver and hasattr(user, "driver_profile"):
            return self.filter(driver=user.driver_profile)
        return self.filter(rider=user)


class Trip(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rider = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="trips_as_rider"
    )
    driver = models.ForeignKey(
        "drivers.DriverProfile",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="trips",
    )
    pickup_latitude = models.DecimalField(**COORDINATE_FIELD_KWARGS, validators=LATITUDE_VALIDATORS)
    pickup_longitude = models.DecimalField(
        **COORDINATE_FIELD_KWARGS, validators=LONGITUDE_VALIDATORS
    )
    pickup_address = models.CharField(max_length=255, blank=True)
    dropoff_latitude = models.DecimalField(
        **COORDINATE_FIELD_KWARGS, validators=LATITUDE_VALIDATORS
    )
    dropoff_longitude = models.DecimalField(
        **COORDINATE_FIELD_KWARGS, validators=LONGITUDE_VALIDATORS
    )
    dropoff_address = models.CharField(max_length=255, blank=True)
    ride_category = models.CharField(max_length=10, choices=RideCategory.choices)
    status = models.CharField(
        max_length=20, choices=TripStatus.choices, default=TripStatus.REQUESTED
    )
    estimated_fare = models.DecimalField(max_digits=10, decimal_places=2)
    estimated_distance_km = models.DecimalField(max_digits=10, decimal_places=3)
    estimated_duration_minutes = models.PositiveIntegerField()
    currency = models.CharField(max_length=3)
    # The upfront price the rider accepted. `estimated_fare` == fare_quote.amount
    # (surge included) and is what the payment charges; the quote keeps the
    # breakdown and the pricing rule version that produced it.
    fare_quote = models.OneToOneField(
        "pricing.FareQuote", on_delete=models.PROTECT, null=True, blank=True, related_name="trip"
    )
    pricing_version = models.PositiveIntegerField(null=True, blank=True)
    surge_multiplier = models.DecimalField(max_digits=4, decimal_places=2, default=1)
    # Optimistic concurrency token: +1 on every status transition. Transitions are
    # conditional UPDATEs on (status, state_version), so a writer acting on a stale
    # read loses cleanly even if it forgot to lock the row.
    state_version = models.PositiveIntegerField(default=0)
    search_started_at = models.DateTimeField(null=True, blank=True)
    driver_assigned_at = models.DateTimeField(null=True, blank=True)
    driver_arrived_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True)
    cancelled_by = models.CharField(max_length=10, choices=ActorType.choices, blank=True)

    objects = TripQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["rider", "-created_at"]),
            models.Index(fields=["driver", "-created_at"]),
            models.Index(fields=["status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=TripStatus.values), name="trip_status_valid"
            ),
            models.CheckConstraint(
                condition=models.Q(ride_category__in=RideCategory.values),
                name="trip_category_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(pickup_latitude__gte=-90, pickup_latitude__lte=90)
                & models.Q(dropoff_latitude__gte=-90, dropoff_latitude__lte=90),
                name="trip_latitude_range",
            ),
            models.CheckConstraint(
                condition=models.Q(pickup_longitude__gte=-180, pickup_longitude__lte=180)
                & models.Q(dropoff_longitude__gte=-180, dropoff_longitude__lte=180),
                name="trip_longitude_range",
            ),
            models.CheckConstraint(
                condition=~(
                    models.Q(pickup_latitude=models.F("dropoff_latitude"))
                    & models.Q(pickup_longitude=models.F("dropoff_longitude"))
                ),
                name="trip_pickup_differs_from_dropoff",
            ),
            models.CheckConstraint(
                condition=models.Q(estimated_fare__gte=0), name="trip_fare_non_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(driver__isnull=False)
                | ~models.Q(status__in=DRIVER_BOUND_STATUSES),
                name="trip_driver_required_for_status",
            ),
            models.CheckConstraint(
                condition=~models.Q(status=TripStatus.CANCELLED) | ~models.Q(cancelled_by=""),
                name="trip_cancelled_by_required",
            ),
            models.CheckConstraint(
                condition=~models.Q(status=TripStatus.COMPLETED)
                | models.Q(completed_at__isnull=False, started_at__isnull=False),
                name="trip_completed_timestamps",
            ),
            # A rider can have at most one active trip at a time.
            models.UniqueConstraint(
                fields=["rider"],
                condition=models.Q(status__in=ACTIVE_STATUSES),
                name="trip_one_active_per_rider",
            ),
            # A driver can serve at most one active trip at a time.
            models.UniqueConstraint(
                fields=["driver"],
                condition=models.Q(status__in=DRIVER_ACTIVE_STATUSES),
                name="trip_one_active_per_driver",
            ),
        ]

    def __str__(self):
        return f"Trip {self.id} [{self.status}]"


class TripEventType(models.TextChoices):
    STATUS_CHANGED = "STATUS_CHANGED", "Status changed"
    OFFER_SENT = "OFFER_SENT", "Offer sent to a driver"
    OFFER_ACCEPTED = "OFFER_ACCEPTED", "Offer accepted"
    OFFER_DECLINED = "OFFER_DECLINED", "Offer declined (rejected) by the driver"
    OFFER_EXPIRED = "OFFER_EXPIRED", "Offer expired unanswered"
    OFFER_CANCELLED = "OFFER_CANCELLED", "Offer withdrawn"
    ASSIGNMENT_FAILED = "ASSIGNMENT_FAILED", "No driver could be assigned"


class TripEvent(models.Model):
    """Append-only audit log of everything that happened to a trip.

    Written in the SAME PostgreSQL transaction as the change it describes (never
    after the fact), so the log cannot disagree with the trip row. STATUS_CHANGED
    rows carry the `state_version` the trip reached; the unique constraint makes
    the status history a gap-free, duplicate-free chain 1..N per trip.
    `event_id` equals the outbox event id where one exists (Kafka correlation).
    """

    id = models.BigAutoField(primary_key=True)
    event_id = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    # No database FK on purpose: an FK insert takes FOR KEY SHARE on the trip row,
    # which would make offer-expiry audit writes (holding offer locks, not the trip
    # lock) wait on Trip and invert the global lock order (Trip -> ... -> Offer).
    # Trips are never deleted (PROTECT in the ORM); reconcile_trips checks orphans.
    trip = models.ForeignKey(
        Trip, on_delete=models.PROTECT, related_name="events", db_constraint=False
    )
    event_type = models.CharField(max_length=20, choices=TripEventType.choices)
    state_version = models.PositiveIntegerField(null=True, blank=True)
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, blank=True)
    actor_type = models.CharField(max_length=10, choices=ActorType.choices)
    actor_id = models.BigIntegerField(null=True, blank=True)  # user / driver profile id
    driver_id = models.BigIntegerField(null=True, blank=True)
    offer_id = models.UUIDField(null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField()

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["trip", "id"], name="trip_event_trip_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["trip", "state_version"],
                condition=models.Q(state_version__isnull=False),
                name="trip_event_one_per_version",
            ),
            models.CheckConstraint(
                condition=models.Q(event_type__in=TripEventType.values),
                name="trip_event_type_valid",
            ),
            models.CheckConstraint(
                condition=~models.Q(event_type="STATUS_CHANGED")
                | models.Q(state_version__isnull=False),
                name="trip_event_status_has_version",
            ),
        ]

    def __str__(self):
        return f"{self.trip_id} #{self.id} {self.event_type} {self.to_status}"
