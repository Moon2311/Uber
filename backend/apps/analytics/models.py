"""Read models built from Kafka events by the `analytics` consumer group.

These are *projections*: derived, rebuildable, never authoritative. Trip and payment
truth lives in apps.trips / apps.payments (PostgreSQL).
"""

from decimal import Decimal

from django.db import models


class TripTimelineEntry(models.Model):
    event_id = models.UUIDField(unique=True)
    trip_id = models.UUIDField(db_index=True)
    event_type = models.CharField(max_length=60)
    status = models.CharField(max_length=20, blank=True)
    occurred_at = models.DateTimeField()

    class Meta:
        ordering = ["occurred_at", "id"]

    def __str__(self):
        return f"{self.trip_id} {self.event_type} {self.status}"


class DriverDailyStats(models.Model):
    driver_id = models.BigIntegerField()
    day = models.DateField()
    completed_trips = models.PositiveIntegerField(default=0)
    gross_estimated_fare = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0")
    )
    went_online = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["driver_id", "day"], name="driver_daily_stats_once")
        ]

    def __str__(self):
        return f"driver {self.driver_id} {self.day}"
