"""Fraud monitoring state (PostgreSQL). Derived from Kafka events and rebuildable
from them, except human decisions (holds, case resolutions), which are authoritative."""

from django.conf import settings
from django.db import models


class Subject(models.TextChoices):
    RIDER = "RIDER", "Rider"  # subject_id = user id
    DRIVER = "DRIVER", "Driver"  # subject_id = driver profile id


class Reliability(models.TextChoices):
    LOW = "LOW", "Low (e.g. GPS: spoofable, noisy)"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High (authoritative records, e.g. payment declines)"


class RiskLevel(models.TextChoices):
    LOW = "LOW", "Low"
    ELEVATED = "ELEVATED", "Elevated (review)"
    HIGH = "HIGH", "High"


class RiskSignal(models.Model):
    subject_type = models.CharField(max_length=10, choices=Subject.choices)
    subject_id = models.BigIntegerField()
    signal = models.CharField(max_length=40)
    reliability = models.CharField(max_length=6, choices=Reliability.choices)
    weight = models.FloatField()
    evidence = models.JSONField(default=dict)
    source_event_id = models.UUIDField()
    observed_at = models.DateTimeField()
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [
            models.Index(
                fields=["subject_type", "subject_id", "expires_at"], name="risk_active_idx"
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["source_event_id", "signal", "subject_type", "subject_id"],
                name="risk_signal_once_per_event",
            )
        ]

    def __str__(self):
        return f"{self.subject_type}:{self.subject_id} {self.signal} ({self.weight})"


class RiskProfile(models.Model):
    subject_type = models.CharField(max_length=10, choices=Subject.choices)
    subject_id = models.BigIntegerField()
    score = models.FloatField(default=0)  # all active signals (capped per signal type)
    blocking_score = models.FloatField(default=0)  # excluding LOW-reliability signals
    level = models.CharField(max_length=10, choices=RiskLevel.choices, default=RiskLevel.LOW)
    signals = models.JSONField(default=dict)  # signal -> points, for explanations
    # Manual hold placed by a reviewer: the only way a subject is blocked without
    # corroborated reliable signals.
    hold = models.BooleanField(default=False)
    hold_reason = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["subject_type", "subject_id"], name="risk_profile_once")
        ]

    def __str__(self):
        return f"{self.subject_type}:{self.subject_id} {self.score:.0f} [{self.level}]"


class CaseStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    DISMISSED = "DISMISSED", "Dismissed"
    ACTIONED = "ACTIONED", "Action taken"


class FraudCase(models.Model):
    """Asynchronous monitoring outcome: something for a human to look at."""

    subject_type = models.CharField(max_length=10, choices=Subject.choices)
    subject_id = models.BigIntegerField()
    status = models.CharField(max_length=10, choices=CaseStatus.choices, default=CaseStatus.OPEN)
    reason = models.CharField(max_length=255)
    score_at_open = models.FloatField()
    opened_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subject_type", "subject_id"],
                condition=models.Q(status="OPEN"),
                name="fraud_one_open_case",
            )  # fmt: skip
        ]

    def __str__(self):
        return f"case {self.pk} {self.subject_type}:{self.subject_id} [{self.status}]"


class Decision(models.TextChoices):
    ALLOW = "ALLOW", "Allow"
    REVIEW = "REVIEW", "Allow, flag for review"
    BLOCK = "BLOCK", "Block"


class FraudDecision(models.Model):
    """Audit of every synchronous (blocking) check."""

    action = models.CharField(max_length=40)
    subject_type = models.CharField(max_length=10, choices=Subject.choices)
    subject_id = models.BigIntegerField()
    decision = models.CharField(max_length=6, choices=Decision.choices)
    score = models.FloatField()
    blocking_score = models.FloatField()
    reasons = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["subject_type", "subject_id", "-created_at"])]

    def __str__(self):
        return f"{self.action} {self.subject_type}:{self.subject_id} -> {self.decision}"


class DriverGpsState(models.Model):
    """Last sampled position seen by the fraud consumer (for speed plausibility)."""

    driver_id = models.BigIntegerField(primary_key=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    recorded_at = models.DateTimeField()

    def __str__(self):
        return f"driver {self.driver_id} @ {self.recorded_at:%H:%M:%S}"
