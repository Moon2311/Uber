"""Blocking check before a trip request (settings.ACTION_GUARDS).

Authoritative data, synchronous, deterministic: a rider with a definitively
FAILED payment must settle it (retry with a working card) before riding again.
Ambiguous (PROCESSING) payments do NOT block: that is our uncertainty, not theirs.
"""

from .models import PaymentRecord, PaymentStatus
from .services import PaymentRequired


def no_outstanding_balance(*, action, user) -> None:
    if PaymentRecord.objects.filter(rider=user, status=PaymentStatus.FAILED).exists():
        raise PaymentRequired()
