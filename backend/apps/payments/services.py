"""Payment lifecycle.

    PENDING ──charge──► PROCESSING ──► PAID ──refund──► REFUNDED
       │                 │   │  ▲
       │                 │   ▼  │ retry (rider fixed the card)
       │                 │  FAILED
       │                 └──► PENDING   provider unreachable: nothing was sent
       └──► CANCELLED (trip cancelled)    PENDING ──► PAID  cash collected

Money-moving calls follow one pattern, so every outcome is recoverable:

  1. txn: lock payment, create a PaymentAttempt with a deterministic idempotency
     key, move to PROCESSING, COMMIT            (intent is durable before the call)
  2. call the provider with that key            (outside any transaction)
  3. txn: lock payment + attempt, apply the answer if nobody else did already

A timeout or crash between 1 and 3 leaves an open attempt; reconciliation asks
the provider about that key (`lookup`) and applies the real outcome, or re-sends
the SAME key if the provider never received it. A second charge is never started
while an earlier one might have taken money (DB constraint: one open attempt per
payment). Webhooks feed the same step 3, deduplicated on the provider event id.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import exceptions, status

from apps.common.exceptions import ConflictError
from apps.common.metrics import metrics

from .models import (
    OPEN_ATTEMPT_STATUSES,
    AttemptKind,
    AttemptStatus,
    PaymentAttempt,
    PaymentMethod,
    PaymentRecord,
    PaymentStatus,
    SavedPaymentMethod,
    WebhookEvent,
)
from .providers import (
    ProviderError,
    ProviderResult,
    ProviderTimeout,
    ProviderUnavailable,
    get_provider,
)
from .signals import payment_status_changed

logger = logging.getLogger("apps.payments")
P = PaymentStatus

TRANSITIONS = {
    (P.PENDING, P.PROCESSING),
    (P.PENDING, P.PAID),  # cash
    (P.PENDING, P.CANCELLED),
    (P.PROCESSING, P.PAID),
    (P.PROCESSING, P.FAILED),
    (P.PROCESSING, P.PENDING),  # provider unreachable: request never sent
    (P.FAILED, P.PROCESSING),  # retry
    (P.PAID, P.REFUNDED),
}
_TIMESTAMP = {P.PAID: "paid_at", P.FAILED: "failed_at", P.REFUNDED: "refunded_at"}


class PaymentRequired(exceptions.APIException):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_detail = "Settle your unpaid trip before requesting a new one."
    default_code = "payment_required"


class StalePayment(ConflictError):
    default_code = "stale_payment_state"


def _transition(payment: PaymentRecord, target: str, **fields) -> PaymentRecord:
    """Compare-and-set status change inside the caller's transaction."""
    if (payment.status, target) not in TRANSITIONS:
        raise ConflictError(f"Payment cannot go from {payment.status} to {target}.")
    now = timezone.now()
    values = {"status": target, "version": payment.version + 1, "updated_at": now, **fields}
    if stamp := _TIMESTAMP.get(target):
        values[stamp] = now
    updated = PaymentRecord.objects.filter(
        pk=payment.pk, status=payment.status, version=payment.version
    ).update(**values)
    if updated != 1:
        raise StalePayment()
    previous = payment.status
    for name, value in values.items():
        setattr(payment, name, value)
    payment_status_changed.send(sender=PaymentRecord, payment=payment, from_status=previous,
                                to_status=target)  # fmt: skip
    metrics().increment("payments.transition", tags={"from": previous, "to": target})
    return payment


# --- lifecycle hooks driven by the trip (called from receivers, trip txn) ---------------
def resolve_method(*, rider, payment_method_id=None) -> SavedPaymentMethod | None:
    methods = SavedPaymentMethod.objects.filter(user=rider, is_active=True)
    if payment_method_id is not None:
        method = methods.filter(pk=payment_method_id).first()
        if method is None:
            raise exceptions.ValidationError({"payment_method_id": ["Unknown payment method."]})
        return method
    return methods.filter(is_default=True).first()


def create_pending_payment(*, trip, payment_method_id=None) -> PaymentRecord:
    """Idempotent: a trip has exactly one payment record (OneToOne)."""
    method = resolve_method(rider=trip.rider, payment_method_id=payment_method_id)
    payment, _ = PaymentRecord.objects.get_or_create(
        trip=trip,
        defaults={
            "rider": trip.rider, "amount": trip.estimated_fare, "currency": trip.currency,
            "status": P.PENDING, "method": PaymentMethod.CARD if method else PaymentMethod.CASH,
            "payment_method": method, "provider": method.provider if method else "cash",
        },
    )  # fmt: skip
    return payment


def cancel_payment_for_trip(*, trip) -> int:
    """Only an un-charged (PENDING) payment is cancelled. Idempotent."""
    payment = PaymentRecord.objects.select_for_update().filter(trip=trip, status=P.PENDING).first()
    if payment is None:
        return 0
    _transition(payment, P.CANCELLED)
    return 1


def on_trip_completed(*, trip) -> None:
    payment = PaymentRecord.objects.select_for_update().filter(trip=trip).first()
    if payment is None or payment.status != P.PENDING:
        return
    if payment.method == PaymentMethod.CASH:
        # Driver collects cash; recorded as paid at completion (upfront price).
        _transition(payment, P.PAID, provider="cash")
    else:
        PaymentRecord.objects.filter(pk=payment.pk).update(due_at=timezone.now())


# --- charging ---------------------------------------------------------------------------
def charge_due_payments(*, limit: int = 20, provider=None) -> int:
    """Worker step: charge card payments of completed trips."""
    now = timezone.now()
    with transaction.atomic():
        ids = list(
            PaymentRecord.objects.select_for_update(skip_locked=True)
            .filter(status=P.PENDING, method=PaymentMethod.CARD, due_at__lte=now)
            .order_by("due_at")
            .values_list("pk", flat=True)[:limit]
        )
        # Push due_at out so a parallel worker skips them while we work (lease).
        PaymentRecord.objects.filter(pk__in=ids).update(due_at=now + timedelta(minutes=5))
    for payment_id in ids:
        charge_payment(payment_id, provider=provider)
    return len(ids)


def charge_payment(payment_id, *, provider=None) -> PaymentRecord:
    provider = provider or get_provider()
    attempt = _start_attempt(payment_id, AttemptKind.CHARGE)
    if attempt is not None:
        _call(provider, attempt)
    return PaymentRecord.objects.get(pk=payment_id)


def _start_attempt(payment_id, kind) -> PaymentAttempt | None:
    """Step 1. Returns None when there is nothing to do (already handled)."""
    with transaction.atomic():
        payment = PaymentRecord.objects.select_for_update().get(pk=payment_id)
        if payment.attempts.filter(status__in=OPEN_ATTEMPT_STATUSES).exists():
            return None  # an earlier request may still move money: reconcile first
        if kind == AttemptKind.CHARGE:
            if payment.status not in (P.PENDING, P.FAILED) or payment.method != PaymentMethod.CARD:
                return None
            if payment.payment_method_id is None or not payment.payment_method.is_active:
                if payment.status == P.PENDING:
                    _transition(payment, P.PROCESSING)
                    _transition(payment, P.FAILED, failure_code="no_payment_method")
                return None
        elif (
            payment.status != P.PAID
            or payment.attempts.filter(
                kind=AttemptKind.REFUND, status=AttemptStatus.SUCCEEDED
            ).exists()
        ):
            return None
        number = payment.attempts.filter(kind=kind).count() + 1
        if kind == AttemptKind.CHARGE and number > settings.PAYMENTS_MAX_CHARGE_ATTEMPTS:
            raise ConflictError("Too many payment attempts; contact support.")
        attempt = PaymentAttempt.objects.create(
            payment=payment,
            kind=kind,
            number=number,
            idempotency_key=f"pay_{payment.pk.hex}_{kind.lower()}_{number}",
            amount=payment.amount,
            currency=payment.currency,
            status=AttemptStatus.SUBMITTED,
            next_check_at=timezone.now()
            + timedelta(seconds=settings.PAYMENTS_SUBMITTED_GRACE_SECONDS),
        )
        if kind == AttemptKind.CHARGE:
            _transition(payment, P.PROCESSING, failure_code="")
    return attempt


def _call(provider, attempt: PaymentAttempt) -> None:
    """Step 2 + 3 (also used to re-send a request the provider never received)."""
    payment = attempt.payment
    try:
        if attempt.kind == AttemptKind.CHARGE:
            result = provider.charge(
                amount=attempt.amount, currency=attempt.currency,
                method_token=payment.payment_method.provider_token,
                idempotency_key=attempt.idempotency_key,
                metadata={"payment_id": str(payment.pk), "trip_id": str(payment.trip_id)},
            )  # fmt: skip
        else:
            result = provider.refund(
                charge_reference=payment.provider_reference, amount=attempt.amount,
                currency=attempt.currency, idempotency_key=attempt.idempotency_key,
            )  # fmt: skip
    except ProviderTimeout:
        metrics().increment("payments.provider.timeout", tags={"kind": attempt.kind})
        mark_ambiguous(attempt.pk, "provider_timeout")
    except ProviderUnavailable:
        metrics().increment("payments.provider.unavailable", tags={"kind": attempt.kind})
        _not_processed(attempt.pk)
    except ProviderError as exc:
        logger.error("payments.provider.rejected", extra={"attempt_id": str(attempt.pk),
                                                          "error": str(exc)[:200]})  # fmt: skip
        apply_result(attempt.pk, ProviderResult(status="failed", failure_code="invalid_request"),
                     review="provider rejected the request")  # fmt: skip
    else:
        apply_result(attempt.pk, result)


def apply_result(attempt_id, result: ProviderResult, *, review: str = "") -> str:
    """Step 3. Idempotent; safe to call from the sync path, webhooks and
    reconciliation in any order. Returns applied | no_change | conflict."""
    with transaction.atomic():
        attempt = PaymentAttempt.objects.select_related("payment").get(pk=attempt_id)
        payment = PaymentRecord.objects.select_for_update().get(pk=attempt.payment_id)  # lock 1st
        attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt_id)
        now = timezone.now()
        final = {"succeeded": AttemptStatus.SUCCEEDED, "failed": AttemptStatus.FAILED}

        if attempt.status in (AttemptStatus.SUCCEEDED, AttemptStatus.FAILED):
            if final.get(result.status) in (attempt.status, None):
                return "no_change"  # duplicate / late "pending"
            return _flag(payment, attempt, f"provider now says {result.status}")
        if result.amount is not None and (
            result.amount != attempt.amount
            or (result.currency or attempt.currency) != attempt.currency
        ):
            return _flag(payment, attempt, "amount/currency mismatch")

        if result.status == "pending":
            attempt.status = AttemptStatus.PENDING
            attempt.provider_reference = result.reference or attempt.provider_reference
            attempt.next_check_at = now + timedelta(seconds=settings.PAYMENTS_PENDING_CHECK_SECONDS)
            attempt.save(update_fields=["status", "provider_reference", "next_check_at"])
            return "applied"

        attempt.status = final[result.status]
        attempt.provider_reference = result.reference or attempt.provider_reference
        attempt.failure_code = result.failure_code
        attempt.resolved_at, attempt.next_check_at = now, None
        attempt.save(update_fields=["status", "provider_reference", "failure_code", "resolved_at",
                                    "next_check_at"])  # fmt: skip
        if attempt.kind == AttemptKind.CHARGE:
            if payment.status != P.PROCESSING:
                return _flag(payment, attempt, f"charge {result.status} while {payment.status}")
            if result.status == "succeeded":
                _transition(payment, P.PAID, provider_reference=attempt.provider_reference)
            else:
                _transition(payment, P.FAILED, failure_code=result.failure_code or "declined")
        elif result.status == "succeeded" and payment.status == P.PAID:
            _transition(payment, P.REFUNDED)
        elif result.status == "failed":
            _flag(payment, attempt, "refund failed")
            return "applied"
        if review:
            _flag(payment, attempt, review)
    metrics().increment("payments.attempt.resolved",
                        tags={"kind": attempt.kind, "status": attempt.status})  # fmt: skip
    return "applied"


def _flag(payment, attempt, reason: str) -> str:
    PaymentRecord.objects.filter(pk=payment.pk).update(needs_review=True,
                                                       review_reason=reason[:255])  # fmt: skip
    metrics().increment("payments.needs_review")
    logger.error("payments.needs_review", extra={"payment_id": str(payment.pk),
                 "attempt_id": str(attempt.pk), "reason": reason})  # fmt: skip
    return "conflict"


def mark_ambiguous(attempt_id, error: str) -> None:
    with transaction.atomic():
        attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.status not in OPEN_ATTEMPT_STATUSES:
            return  # a webhook resolved it meanwhile
        attempt.status, attempt.error = AttemptStatus.UNKNOWN, error
        attempt.next_check_at = timezone.now() + timedelta(seconds=_check_backoff(attempt.checks))
        attempt.save(update_fields=["status", "error", "next_check_at"])


def _not_processed(attempt_id) -> None:
    """The provider refused the connection: nothing happened, try again later."""
    with transaction.atomic():
        attempt = PaymentAttempt.objects.select_related("payment").get(pk=attempt_id)
        payment = PaymentRecord.objects.select_for_update().get(pk=attempt.payment_id)
        attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.status != AttemptStatus.SUBMITTED:
            return
        attempt.status, attempt.failure_code = AttemptStatus.FAILED, "provider_unavailable"
        attempt.resolved_at, attempt.next_check_at = timezone.now(), None
        attempt.save(update_fields=["status", "failure_code", "resolved_at", "next_check_at"])
        if attempt.kind == AttemptKind.CHARGE and payment.status == P.PROCESSING:
            retry_at = timezone.now() + timedelta(
                seconds=settings.PAYMENTS_UNAVAILABLE_RETRY_SECONDS
            )
            _transition(payment, P.PENDING, due_at=retry_at)


def _check_backoff(checks: int) -> float:
    return min(settings.PAYMENTS_RECONCILE_MAX_SECONDS,
               settings.PAYMENTS_RECONCILE_BASE_SECONDS * 2**checks)  # fmt: skip


# --- reconciliation of ambiguous / pending outcomes -------------------------------------
def reconcile_open_attempts(*, limit: int = 50, provider=None, now=None) -> int:
    """Worker step: ask the provider what happened to open attempts that are due."""
    provider = provider or get_provider()
    now = now or timezone.now()
    with transaction.atomic():
        due = list(
            PaymentAttempt.objects.select_for_update(skip_locked=True)
            .filter(status__in=OPEN_ATTEMPT_STATUSES, next_check_at__lte=now)
            .order_by("next_check_at")[:limit]
        )
        for attempt in due:  # lease: other workers skip these for a while
            attempt.checks += 1
            attempt.next_check_at = now + timedelta(seconds=_check_backoff(attempt.checks))
            attempt.save(update_fields=["checks", "next_check_at"])
    for attempt in due:
        _reconcile(provider, attempt)
    return len(due)


def _reconcile(provider, attempt: PaymentAttempt) -> None:
    try:
        found = provider.lookup(idempotency_key=attempt.idempotency_key)
    except (ProviderTimeout, ProviderUnavailable):
        return  # try again at next_check_at
    if found is not None and found.status != "pending":
        apply_result(attempt.pk, found)
        metrics().increment("payments.reconcile.resolved", tags={"status": found.status})
        return
    if attempt.checks >= settings.PAYMENTS_MAX_RECONCILE_CHECKS:
        PaymentAttempt.objects.filter(pk=attempt.pk).update(next_check_at=None)
        _flag(attempt.payment, attempt, "outcome still unknown after reconciliation")
        return
    if found is None and attempt.status in (AttemptStatus.SUBMITTED, AttemptStatus.UNKNOWN):
        # The provider never got it: re-send with the SAME key (cannot double charge).
        metrics().increment("payments.reconcile.resubmitted")
        _call(provider, PaymentAttempt.objects.select_related("payment__payment_method").get(
            pk=attempt.pk))  # fmt: skip


# --- rider / staff actions ---------------------------------------------------------------
def retry_payment(*, payment_id, rider, payment_method_id=None, provider=None) -> PaymentRecord:
    with transaction.atomic():
        payment = PaymentRecord.objects.select_for_update().filter(pk=payment_id,
                                                                   rider=rider).first()  # fmt: skip
        if payment is None:
            raise PaymentRecord.DoesNotExist
        if payment.status != P.FAILED:
            raise ConflictError(f"Only a failed payment can be retried (now {payment.status}).")
        if payment_method_id is not None:
            method = resolve_method(rider=rider, payment_method_id=payment_method_id)
            PaymentRecord.objects.filter(pk=payment.pk).update(
                payment_method=method, method=PaymentMethod.CARD, provider=method.provider
            )
    return charge_payment(payment_id, provider=provider)


def refund_payment(*, payment_id, provider=None) -> PaymentRecord:
    """Full refund. Idempotent: one successful refund per payment."""
    provider = provider or get_provider()
    payment = PaymentRecord.objects.get(pk=payment_id)
    if payment.status == P.REFUNDED:
        return payment
    if payment.status != P.PAID or payment.method != PaymentMethod.CARD:
        raise ConflictError("Only a paid card payment can be refunded here.")
    attempt = _start_attempt(payment_id, AttemptKind.REFUND)
    if attempt is None:
        raise ConflictError("A refund for this payment is already in progress.")
    _call(provider, attempt)
    return PaymentRecord.objects.get(pk=payment_id)


# --- webhooks ------------------------------------------------------------------------------
WEBHOOK_RESULTS = {
    "charge.succeeded": "succeeded", "charge.failed": "failed", "charge.pending": "pending",
    "refund.succeeded": "succeeded", "refund.failed": "failed",
}  # fmt: skip


def process_webhook(provider_name: str, body: bytes, headers) -> str:
    """Raises InvalidSignature / ProviderError (-> 400). Returns the outcome."""
    provider = get_provider(provider_name)
    payload = provider.parse_webhook(body, headers)  # signature verified on raw bytes
    try:
        with transaction.atomic():
            event = WebhookEvent.objects.create(
                provider=provider.name, event_id=payload.event_id, event_type=payload.event_type,
                payload=payload.raw, outcome="received",
            )  # fmt: skip
            event.outcome, event.detail = _apply_webhook(payload)
            event.save(update_fields=["outcome", "detail"])
    except IntegrityError:
        metrics().increment("payments.webhook.duplicate")
        return "duplicate"
    metrics().increment("payments.webhook", tags={"outcome": event.outcome})
    return event.outcome


def _apply_webhook(payload) -> tuple[str, str]:
    status_ = WEBHOOK_RESULTS.get(payload.event_type)
    if status_ is None:
        return "ignored", "unhandled event type"
    attempt = PaymentAttempt.objects.filter(idempotency_key=payload.idempotency_key).first()
    if attempt is None and payload.reference:
        attempt = PaymentAttempt.objects.filter(provider_reference=payload.reference).first()
    if attempt is None:
        return "ignored", "unknown payment"
    if attempt.kind.lower() != payload.event_type.split(".")[0]:
        return "ignored", "event kind does not match attempt"
    result = ProviderResult(
        status=status_,
        reference=payload.reference,
        amount=payload.amount,
        currency=payload.currency,
        failure_code=payload.failure_code,
    )
    return apply_result(attempt.pk, result), str(attempt.pk)
