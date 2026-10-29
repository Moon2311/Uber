"""Synchronous fraud checks on sensitive actions (settings.ACTION_GUARDS).

Only checks that MUST block run here; scoring itself happens asynchronously in
the `fraud` consumer. Failure behaviour is explicit per action
(FRAUD_POLICY["fail_open"]): if the check itself breaks, a trip request is
allowed (availability; payment still has to succeed) while adding a payment
method is refused (the classic card-testing target).
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import exceptions, status

from apps.common.exceptions import ServiceUnavailableError
from apps.common.metrics import metrics

from .models import Decision, FraudDecision, Subject
from .scoring import decide, policy

logger = logging.getLogger("apps.fraud")


class ActionBlocked(exceptions.APIException):
    status_code = status.HTTP_403_FORBIDDEN
    # Never reveal scores or which signal fired (that teaches evasion).
    default_detail = "This action is not available on your account right now. Contact support."
    default_code = "action_blocked"


def _velocity_block(action, user) -> str | None:
    """Deterministic hard limits on authoritative data (rate-limit style)."""
    if action == "add_payment_method":
        from apps.payments.models import SavedPaymentMethod

        cfg = settings.FRAUD_RULES["card_testing"]
        added = SavedPaymentMethod.objects.filter(
            user=user, created_at__gte=timezone.now() - timedelta(hours=cfg["window_hours"])
        ).count()
        if added >= cfg["threshold"]:
            return "card_testing_velocity"
    return None


def check(*, action: str, user) -> None:
    try:
        reason = _velocity_block(action, user)
        if reason:
            assessment = (Decision.BLOCK, 0.0, 0.0, [reason])
        else:
            a = decide(Subject.RIDER, user.pk)
            assessment = (a.decision, a.score, a.blocking_score, a.reasons)
    except Exception:  # noqa: BLE001 - the check failing must not be mistaken for a verdict
        logger.exception("fraud.check.error", extra={"action": action})
        metrics().increment("fraud.check.error", tags={"action": action})
        if policy()["fail_open"].get(action, False):
            return
        raise ServiceUnavailableError() from None
    decision, score, blocking, reasons = assessment
    metrics().increment("fraud.decision", tags={"action": action, "decision": decision})

    def record():
        FraudDecision.objects.create(action=action, subject_type=Subject.RIDER,
                                     subject_id=user.pk, decision=decision, score=score,
                                     blocking_score=blocking, reasons=reasons)  # fmt: skip

    if decision != Decision.BLOCK:
        record()  # commits with the action it allowed
        return
    blocked = ActionBlocked()
    if transaction.get_connection().in_atomic_block:
        # Raising rolls the caller's transaction back, and a row written now with it:
        # the API exception handler records the BLOCK after the rollback instead.
        blocked.record_after_rollback = record
    else:
        record()
    raise blocked
