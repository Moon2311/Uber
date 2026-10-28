import uuid

from django.conf import settings
from django.db import models

from apps.common.models import TimestampedModel


class PaymentStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    AUTHORIZED = "AUTHORIZED", "Authorized"
    CAPTURED = "CAPTURED", "Captured"
    FAILED = "FAILED", "Failed"
    REFUNDED = "REFUNDED", "Refunded"
    CANCELLED = "CANCELLED", "Cancelled"


class PaymentMethod(models.TextChoices):
    CASH = "CASH", "Cash"
    CARD = "CARD", "Card"


class PaymentRecord(TimestampedModel):
    """Ledger-style record of what is owed for a trip.

    Card data is never stored here; `provider_reference` holds only the opaque id
    returned by a PCI-compliant payment provider (integrated in a later step).
    """

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
    provider = models.CharField(max_length=50, blank=True)
    provider_reference = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gte=0), name="payment_amount_non_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=PaymentStatus.values), name="payment_status_valid"
            ),
        ]
        indexes = [models.Index(fields=["rider", "-created_at"])]

    def __str__(self):
        return f"Payment {self.id} {self.amount} {self.currency} [{self.status}]"
