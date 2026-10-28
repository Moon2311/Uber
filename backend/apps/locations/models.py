from django.db import models

from apps.common.geo import COORDINATE_FIELD_KWARGS, LATITUDE_VALIDATORS, LONGITUDE_VALIDATORS


class DriverLocation(models.Model):
    """Latest known position per driver: exactly one row per driver, upserted.

    Written only through a `LocationStore` (see stores.py), which guarantees that
    an update never overwrites a newer fix (`recorded_at` is monotonic per driver).
    """

    driver = models.OneToOneField(
        "drivers.DriverProfile",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="location",
    )
    latitude = models.DecimalField(**COORDINATE_FIELD_KWARGS, validators=LATITUDE_VALIDATORS)
    longitude = models.DecimalField(**COORDINATE_FIELD_KWARGS, validators=LONGITUDE_VALIDATORS)
    heading = models.FloatField(null=True, blank=True)
    speed_kmh = models.FloatField(null=True, blank=True)
    accuracy_m = models.FloatField(null=True, blank=True)
    # Device fix time, clamped to never exceed server receipt time. Drives
    # ordering (out-of-order protection) and freshness (staleness).
    recorded_at = models.DateTimeField(db_index=True)
    # Server time the latest applied update was received.
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["latitude", "longitude"], name="driverloc_lat_lng_idx")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(latitude__gte=-90) & models.Q(latitude__lte=90),
                name="driverloc_latitude_range",
            ),
            models.CheckConstraint(
                condition=models.Q(longitude__gte=-180) & models.Q(longitude__lte=180),
                name="driverloc_longitude_range",
            ),
            models.CheckConstraint(
                condition=models.Q(heading__isnull=True)
                | (models.Q(heading__gte=0) & models.Q(heading__lt=360)),
                name="driverloc_heading_range",
            ),
            models.CheckConstraint(
                condition=models.Q(speed_kmh__isnull=True) | models.Q(speed_kmh__gte=0),
                name="driverloc_speed_non_negative",
            ),
        ]

    def __str__(self):
        return f"{self.driver_id} @ {self.latitude},{self.longitude}"


class DriverLocationEvent(models.Model):
    """Optional, *sampled* location history (route audit, analytics, disputes).

    A row is written only when the driver has moved or enough time has passed
    since the previous event (LOCATION_HISTORY_* settings) and rows are pruned
    after LOCATION_HISTORY_RETENTION_DAYS. It is never read on the hot path.
    """

    id = models.BigAutoField(primary_key=True)
    driver = models.ForeignKey(
        "drivers.DriverProfile", on_delete=models.CASCADE, related_name="location_events"
    )
    latitude = models.DecimalField(**COORDINATE_FIELD_KWARGS)
    longitude = models.DecimalField(**COORDINATE_FIELD_KWARGS)
    heading = models.FloatField(null=True, blank=True)
    speed_kmh = models.FloatField(null=True, blank=True)
    accuracy_m = models.FloatField(null=True, blank=True)
    recorded_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["driver", "-recorded_at"], name="locevent_driver_recent_idx"),
            models.Index(fields=["recorded_at"], name="locevent_recorded_at_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(latitude__gte=-90, latitude__lte=90)
                & models.Q(longitude__gte=-180, longitude__lte=180),
                name="locevent_coordinates_range",
            ),
        ]

    def __str__(self):
        return f"{self.driver_id} @ {self.recorded_at:%Y-%m-%d %H:%M:%S}"
