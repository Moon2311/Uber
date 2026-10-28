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
    driver_assigned_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True)

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
