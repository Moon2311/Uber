import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models.functions import Upper

from apps.common.models import TimestampedModel


class RideCategory(models.TextChoices):
    ECONOMY = "ECONOMY", "Economy"
    COMFORT = "COMFORT", "Comfort"
    XL = "XL", "XL"


class AvailabilityStatus(models.TextChoices):
    OFFLINE = "OFFLINE", "Offline"
    ONLINE = "ONLINE", "Online"


class DriverProfile(TimestampedModel):
    # Opaque identifier safe to expose to riders (no enumeration of internal ids).
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="driver_profile"
    )
    license_number = models.CharField(max_length=50, unique=True)
    availability_status = models.CharField(
        max_length=10, choices=AvailabilityStatus.choices, default=AvailabilityStatus.OFFLINE
    )
    availability_changed_at = models.DateTimeField(null=True, blank=True)
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("5.00"))

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(availability_status__in=AvailabilityStatus.values),
                name="driver_availability_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(rating__gte=0) & models.Q(rating__lte=5),
                name="driver_rating_range",
            ),
        ]
        indexes = [models.Index(fields=["availability_status"])]

    def __str__(self):
        return f"Driver<{self.user.email}>"

    @property
    def is_online(self) -> bool:
        return self.availability_status == AvailabilityStatus.ONLINE

    @property
    def active_vehicle(self):
        return self.vehicles.filter(is_active=True).first()


class Vehicle(TimestampedModel):
    driver = models.ForeignKey(DriverProfile, on_delete=models.CASCADE, related_name="vehicles")
    make = models.CharField(max_length=50)
    model = models.CharField(max_length=50)
    color = models.CharField(max_length=30)
    plate_number = models.CharField(max_length=20)
    year = models.PositiveSmallIntegerField()
    category = models.CharField(max_length=10, choices=RideCategory.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(Upper("plate_number"), name="vehicle_plate_ci_unique"),
            models.UniqueConstraint(
                fields=["driver"],
                condition=models.Q(is_active=True),
                name="vehicle_one_active_per_driver",
            ),
            models.CheckConstraint(
                condition=models.Q(category__in=RideCategory.values),
                name="vehicle_category_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(year__gte=1990) & models.Q(year__lte=2100),
                name="vehicle_year_range",
            ),
        ]

    def __str__(self):
        return f"{self.make} {self.model} ({self.plate_number})"
