"""Payment records. PostgreSQL is authoritative for what we BELIEVE happened; the
provider is authoritative for what actually happened to money, and
reconciliation (services.reconcile_*) closes the gap when the two may differ.

No raw card data is ever stored or accepted: a PaymentMethod holds only the
provider's opaque reusable token plus display data (brand, last4, expiry), which
PCI DSS allows outside the cardholder-data environment.
"""

import uuid

from django.conf import settings
from django.db import models

from apps.common.models import TimestampedModel


class PaymentStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"  # trip not finished / charge not attempted yet
    PROCESSING = "PROCESSING", "Processing"  # submitted; outcome pending or ambiguous
    PAID = "PAID", "Paid"
    FAILED = "FAILED", "Failed"  # definitively declined; rider must fix and retry
    REFUNDED = "REFUNDED", "Refunded"
    CANCELLED = "CANCELLED", "Cancelled"  # trip cancelled before any charge


class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Cash"
    CARD = "CARD", "Card"


class SavedPaymentMethod(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payment_methods"
    )
    provider = models.CharField(max_length=50)
    # Provider's reusable payment-method id. Useless without our provider secret,
    # never returned by the API, never logged.
    provider_token = models.CharField(max_length=255)
    brand = models.CharField(max_length=20, blank=True)
    last4 = models.CharField(max_length=4, blank=True)
    exp_month = models.PositiveSmallIntegerField(null=True, blank=True)
    exp_year = models.PositiveSmallIntegerField(null=True, blank=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(is_default=True, is_active=True),
                name="payment_method_one_default",
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_token"], name="payment_method_token_once"
            ),  # fmt: skip
        ]

    def __str__(self):
        return f"{self.brand} ****{self.last4}"


class PaymentRecord(TimestampedModel):
    """What is owed for a trip and where collecting it stands. One per trip."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trip = models.OneToOneField("trips.Trip", on_delete=models.PROTECT, related_name="payment")
    rider = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments"
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(
        max_length=12, choices=PaymentStatus.choices, default=PaymentStatus.PENDING
    )
    method = models.CharField(
        max_length=10, choices=PaymentMethod.choices, default=PaymentMethod.CASH
    )
    payment_method = models.ForeignKey(
        SavedPaymentMethod, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    provider = models.CharField(max_length=50, blank=True)
    provider_reference = models.CharField(max_length=255, blank=True)  # charge id
    version = models.PositiveIntegerField(default=0)  # compare-and-set token
    due_at = models.DateTimeField(null=True, blank=True)  # set when the trip completes
    paid_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=60, blank=True)
    # Reconciliation could not settle an ambiguous outcome, or the provider
    # reported something inconsistent: a human must look before money moves again.
    needs_review = models.BooleanField(default=False)
    review_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gte=0), name="payment_amount_non_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=PaymentStatus.values), name="payment_status_valid"
            ),
            models.CheckConstraint(
                condition=~models.Q(status="PAID") | models.Q(paid_at__isnull=False),
                name="payment_paid_has_timestamp",
            ),
            models.CheckConstraint(
                condition=~models.Q(method="CARD", status__in=["PROCESSING", "PAID", "REFUNDED"])
                | models.Q(payment_method__isnull=False),
                name="payment_card_has_method",
            ),
        ]
        indexes = [
            models.Index(fields=["rider", "-created_at"]),
            models.Index(
                fields=["due_at"],
                name="payment_due_idx",
                condition=models.Q(status="PENDING", due_at__isnull=False),
            ),  # fmt: skip
        ]

    def __str__(self):
        return f"Payment {self.id} {self.amount} {self.currency} [{self.status}]"


class AttemptKind(models.TextChoices):
    CHARGE = "CHARGE", "Charge"
    REFUND = "REFUND", "Refund"


class AttemptStatus(models.TextChoices):
    SUBMITTED = "SUBMITTED", "Submitted (no answer yet)"
    PENDING = "PENDING", "Accepted, provider still processing"
    UNKNOWN = "UNKNOWN", "Ambiguous (timeout / lost response)"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"


OPEN_ATTEMPT_STATUSES = [AttemptStatus.SUBMITTED, AttemptStatus.PENDING, AttemptStatus.UNKNOWN]


class PaymentAttempt(models.Model):
    """One request to the provider. Written (and committed) BEFORE the call, with
    the idempotency key the provider will see, so a crash or timeout always leaves
    a record that reconciliation can resolve by asking the provider."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(PaymentRecord, on_delete=models.PROTECT, related_name="attempts")
    kind = models.CharField(max_length=6, choices=AttemptKind.choices)
    number = models.PositiveSmallIntegerField()
    idempotency_key = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=10, choices=AttemptStatus.choices)
    provider_reference = models.CharField(max_length=255, blank=True)
    failure_code = models.CharField(max_length=60, blank=True)
    error = models.CharField(max_length=255, blank=True)  # sanitized; never provider secrets
    checks = models.PositiveSmallIntegerField(default=0)  # reconciliation lookups made
    next_check_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "kind", "number"], name="attempt_once"),
            # At most one unresolved attempt per payment: never start a second charge
            # while the first one might still have taken money.
            models.UniqueConstraint(
                fields=["payment"],
                condition=models.Q(status__in=OPEN_ATTEMPT_STATUSES),
                name="attempt_one_open_per_payment",
            ),  # fmt: skip
        ]
        indexes = [
            models.Index(
                fields=["next_check_at"],
                name="attempt_open_check_idx",
                condition=models.Q(status__in=OPEN_ATTEMPT_STATUSES),
            )  # fmt: skip
        ]

    def __str__(self):
        return f"{self.kind} #{self.number} {self.idempotency_key} [{self.status}]"


class WebhookEvent(models.Model):
    """Every verified provider callback, deduplicated on the provider's event id."""

    provider = models.CharField(max_length=50)
    event_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=60)
    payload = models.JSONField()  # provider data object only; no secrets, no card data
    received_at = models.DateTimeField(auto_now_add=True)
    outcome = models.CharField(max_length=30)  # applied | no_change | ignored | conflict
    detail = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "event_id"], name="webhook_once")
        ]

    def __str__(self):
        return f"{self.provider}:{self.event_id} {self.event_type} -> {self.outcome}"


class SandboxCharge(models.Model):
    """State of the SANDBOX provider (stands in for the provider's own database, so
    idempotency and lookups behave like a real provider across processes)."""

    reference = models.CharField(max_length=40, unique=True)
    idempotency_key = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=6)  # charge | refund
    status = models.CharField(max_length=12)  # succeeded | failed | pending | not_received
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    token = models.CharField(max_length=255, blank=True)
    charge_reference = models.CharField(max_length=40, blank=True)  # for refunds
    failure_code = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"sandbox {self.kind} {self.reference} [{self.status}]"
