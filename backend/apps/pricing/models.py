"""Pricing records. PostgreSQL owns all of them.

* PricingRuleSet - immutable, versioned rules; exactly one ACTIVE at a time.
* FareQuote      - an upfront price shown to a rider, with the rule version and
                   surge multiplier that produced it. A trip consumes at most one
                   quote and a quote is consumed by at most one trip (OneToOne on
                   Trip.fare_quote).
* SurgeZone      - append-only history of computed surge multipliers per zone.
"""

import uuid

from django.conf import settings
from django.db import models

from apps.common.geo import COORDINATE_FIELD_KWARGS


class RuleSetStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    RETIRED = "RETIRED", "Retired"


class PricingRuleSet(models.Model):
    version = models.PositiveIntegerField(unique=True)
    status = models.CharField(max_length=10, choices=RuleSetStatus.choices)
    rules = models.JSONField()  # validated by rules.parse() before insert
    notes = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField()
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["status"], condition=models.Q(status="ACTIVE"), name="pricing_one_active"
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=RuleSetStatus.values), name="pricing_status_valid"
            ),
        ]

    _MUTABLE = {"status", "retired_at"}

    def __str__(self):
        return f"pricing v{self.version} [{self.status}]"

    def save(self, *args, update_fields=None, **kwargs):
        # Published rules are immutable: only retirement may change an existing row.
        if self.pk is not None and (update_fields is None or set(update_fields) - self._MUTABLE):
            raise ValueError("Pricing rule sets are immutable; publish a new version")
        super().save(*args, update_fields=update_fields, **kwargs)


class FareQuote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rider = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fare_quotes"
    )
    rule_set = models.ForeignKey(PricingRuleSet, on_delete=models.PROTECT, related_name="quotes")
    ride_category = models.CharField(max_length=10)
    pickup_latitude = models.DecimalField(**COORDINATE_FIELD_KWARGS)
    pickup_longitude = models.DecimalField(**COORDINATE_FIELD_KWARGS)
    dropoff_latitude = models.DecimalField(**COORDINATE_FIELD_KWARGS)
    dropoff_longitude = models.DecimalField(**COORDINATE_FIELD_KWARGS)
    distance_km = models.DecimalField(max_digits=10, decimal_places=3)
    duration_minutes = models.PositiveIntegerField()
    surge_zone = models.CharField(max_length=20, blank=True)
    surge_multiplier = models.DecimalField(max_digits=4, decimal_places=2)
    base_amount = models.DecimalField(max_digits=10, decimal_places=2)  # before surge
    amount = models.DecimalField(max_digits=10, decimal_places=2)  # what the rider pays
    currency = models.CharField(max_length=3)
    breakdown = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(fields=["surge_zone", "created_at"], name="quote_zone_time_idx"),
            models.Index(fields=["rider", "-created_at"], name="quote_rider_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gte=0) & models.Q(base_amount__gte=0),
                name="quote_amount_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(surge_multiplier__gte=1), name="quote_surge_at_least_one"
            ),
        ]

    def __str__(self):
        return f"Quote {self.id} {self.amount} {self.currency} x{self.surge_multiplier}"


class SurgeZone(models.Model):
    id = models.BigAutoField(primary_key=True)
    zone = models.CharField(max_length=20)  # H3 cell at PRICING_SURGE_H3_RESOLUTION
    multiplier = models.DecimalField(max_digits=4, decimal_places=2)
    demand = models.PositiveIntegerField()  # distinct riders quoting in the window
    supply = models.PositiveIntegerField()  # free, online, fresh drivers in the zone
    rule_set_version = models.PositiveIntegerField()
    computed_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["zone", "-computed_at"], name="surge_zone_latest_idx")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(multiplier__gte=1), name="surge_multiplier_at_least_one"
            )
        ]

    def __str__(self):
        return f"{self.zone} x{self.multiplier} @ {self.computed_at:%H:%M:%S}"
