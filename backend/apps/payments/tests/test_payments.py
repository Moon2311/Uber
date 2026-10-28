"""Payment lifecycle against the sandbox provider: success, declines, timeouts,
lost requests, async settlement via webhooks, duplicate callbacks, refunds."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.events.models import OutboxEvent
from apps.payments.models import (
    AttemptStatus,
    PaymentAttempt,
    PaymentRecord,
    PaymentStatus,
    SandboxCharge,
    WebhookEvent,
)
from apps.payments.providers.sandbox import SandboxProvider, webhook_request
from apps.payments.services import (
    PaymentRequired,
    charge_due_payments,
    reconcile_open_attempts,
    refund_payment,
    retry_payment,
)
from apps.trips.services import cancel_trip_by_rider
from apps.trips.tests.conftest import new_trip
from conftest import client_for, make_rider

from .conftest import card, completed_trip

pytestmark = pytest.mark.django_db
P = PaymentStatus


def later(seconds=3600):
    return timezone.now() + timedelta(seconds=seconds)


def statuses(payment):
    return list(
        OutboxEvent.objects.filter(event_type="PaymentStatusChanged", key=str(payment.trip_id))
        .order_by("id")
        .values_list("payload__to_status", flat=True)
    )


def post_webhook(body, headers):
    from rest_framework.test import APIClient

    client = APIClient()
    return client.post(
        reverse("v1:payment-webhook", kwargs={"provider": "sandbox"}),
        body,
        content_type="application/json",
        **{f"HTTP_{k.upper().replace('-', '_')}": v for k, v in headers.items()},
    )


# --- happy paths ----------------------------------------------------------------------------------
def test_card_payment_is_charged_after_completion(provider):
    trip, payment = completed_trip()
    assert payment.status == P.PENDING and payment.due_at is not None and payment.method == "CARD"
    assert charge_due_payments(provider=provider) == 1
    payment.refresh_from_db()
    assert payment.status == P.PAID and payment.paid_at and payment.provider_reference
    [attempt] = payment.attempts.all()
    assert attempt.status == AttemptStatus.SUCCEEDED and attempt.amount == trip.estimated_fare
    assert attempt.idempotency_key == f"pay_{payment.pk.hex}_charge_1"
    assert statuses(payment) == [P.PROCESSING, P.PAID]
    assert charge_due_payments(provider=provider) == 0  # nothing charged twice


def test_cash_trip_is_paid_at_completion():
    _, payment = completed_trip(token=None)
    assert payment.method == "CASH" and payment.status == P.PAID and payment.provider == "cash"
    assert not payment.attempts.exists()


def test_cancelled_trip_cancels_the_pending_payment():
    rider = make_rider()
    card(rider)
    trip = new_trip(rider)
    cancel_trip_by_rider(trip_id=trip.pk, rider=rider)
    assert PaymentRecord.objects.get(trip=trip).status == P.CANCELLED


# --- declines -------------------------------------------------------------------------------------
def test_decline_fails_blocks_new_trips_until_retried_with_a_good_card(provider):
    trip, payment = completed_trip("tok_sandbox_decline")
    charge_due_payments(provider=provider)
    payment.refresh_from_db()
    assert payment.status == P.FAILED and payment.failure_code == "card_declined"
    with pytest.raises(PaymentRequired):
        new_trip(trip.rider)  # blocking guard: unpaid balance

    good = card(trip.rider, "tok_sandbox_mastercard")
    payment = retry_payment(payment_id=payment.pk, rider=trip.rider, payment_method_id=good.pk,
                            provider=provider)  # fmt: skip
    assert payment.status == P.PAID and payment.attempts.count() == 2
    new_trip(trip.rider)  # allowed again


def test_completing_without_a_usable_card_fails_cleanly(provider):
    trip, payment = completed_trip()
    payment.payment_method.is_active = False
    payment.payment_method.save()
    charge_due_payments(provider=provider)
    payment.refresh_from_db()
    assert payment.status == P.FAILED and payment.failure_code == "no_payment_method"
    assert not payment.attempts.exists()  # nothing was sent to the provider


# --- timeouts and ambiguous outcomes --------------------------------------------------------------
def test_timeout_after_charge_is_reconciled_without_double_charging(provider):
    _, payment = completed_trip("tok_sandbox_timeout")
    charge_due_payments(provider=provider)
    payment.refresh_from_db()
    [attempt] = payment.attempts.all()
    assert payment.status == P.PROCESSING and attempt.status == AttemptStatus.UNKNOWN
    # A second worker pass must NOT start another charge while the first is unknown.
    PaymentRecord.objects.filter(pk=payment.pk).update(due_at=timezone.now())
    charge_due_payments(provider=provider)
    assert payment.attempts.count() == 1

    assert reconcile_open_attempts(provider=provider, now=later()) == 1
    payment.refresh_from_db()
    assert payment.status == P.PAID
    assert SandboxCharge.objects.filter(kind="charge").count() == 1  # money moved once


def test_lost_request_is_resent_with_the_same_idempotency_key(provider):
    _, payment = completed_trip("tok_sandbox_lost_request")
    charge_due_payments(provider=provider)
    [attempt] = PaymentAttempt.objects.filter(payment=payment)
    assert attempt.status == AttemptStatus.UNKNOWN
    assert provider.lookup(idempotency_key=attempt.idempotency_key) is None  # never arrived

    reconcile_open_attempts(provider=provider, now=later())
    payment.refresh_from_db()
    assert payment.status == P.PAID and payment.attempts.count() == 1
    [charge] = SandboxCharge.objects.filter(kind="charge")
    assert charge.idempotency_key == attempt.idempotency_key


def test_crash_between_submit_and_answer_is_recovered(provider, monkeypatch):
    _, payment = completed_trip()

    def crash(*a, **kw):
        raise SystemExit("worker killed mid-call")  # not an Exception: nothing handles it

    monkeypatch.setattr(SandboxProvider, "charge", crash)
    with pytest.raises(SystemExit):
        charge_due_payments(provider=SandboxProvider())
    monkeypatch.undo()
    [attempt] = payment.attempts.all()
    assert attempt.status == AttemptStatus.SUBMITTED  # intent was committed before the call
    reconcile_open_attempts(provider=provider, now=later())  # provider never saw it -> resend
    payment.refresh_from_db()
    assert payment.status == P.PAID


def test_provider_unreachable_means_not_charged_and_retried_later(provider):
    _, payment = completed_trip("tok_sandbox_unavailable")
    charge_due_payments(provider=provider)
    payment.refresh_from_db()
    assert payment.status == P.PENDING and payment.due_at > timezone.now()
    assert payment.attempts.get().failure_code == "provider_unavailable"
    assert statuses(payment) == [P.PROCESSING, P.PENDING]


def test_outcome_still_unknown_after_max_checks_needs_a_human(provider, settings, monkeypatch):
    settings.PAYMENTS_MAX_RECONCILE_CHECKS = 2
    _, payment = completed_trip("tok_sandbox_async")  # stays pending at the provider
    charge_due_payments(provider=provider)
    for i in range(3):
        reconcile_open_attempts(provider=provider, now=later(10_000 * (i + 1)))
    payment.refresh_from_db()
    assert payment.status == P.PROCESSING and payment.needs_review
    assert "unknown" in payment.review_reason
    assert payment.attempts.get().next_check_at is None  # no more automatic checks


# --- webhooks -------------------------------------------------------------------------------------
def test_async_charge_settles_via_signed_webhook_and_duplicates_are_ignored(provider):
    _, payment = completed_trip("tok_sandbox_async")
    charge_due_payments(provider=provider)
    attempt = payment.attempts.get()
    assert attempt.status == AttemptStatus.PENDING

    body, headers = SandboxProvider.settle_async(attempt.provider_reference, "succeeded",
                                                 event_id="evt_settle_1")  # fmt: skip
    first = post_webhook(body, headers)
    assert first.status_code == 200 and first.json() == {"status": "applied"}
    payment.refresh_from_db()
    assert payment.status == P.PAID

    again = post_webhook(body, headers)  # provider retries the same event
    assert again.status_code == 200 and again.json() == {"status": "duplicate"}
    resent, resent_headers = webhook_request(SandboxCharge.objects.get(), event_id="evt_settle_2")
    assert post_webhook(resent, resent_headers).json() == {
        "status": "no_change"
    }  # new id, same fact
    assert statuses(payment) == [P.PROCESSING, P.PAID]  # exactly one PAID transition
    assert WebhookEvent.objects.count() == 2


def test_async_decline_via_webhook():
    _, payment = completed_trip("tok_sandbox_async")
    charge_due_payments(provider=SandboxProvider())
    ref = payment.attempts.get().provider_reference
    post_webhook(*SandboxProvider.settle_async(ref, "failed", failure_code="insufficient_funds"))
    payment.refresh_from_db()
    assert payment.status == P.FAILED and payment.failure_code == "insufficient_funds"


def test_forged_or_tampered_webhooks_change_nothing():
    _, payment = completed_trip("tok_sandbox_async")
    charge_due_payments(provider=SandboxProvider())
    row = SandboxCharge.objects.get()
    row.status = "succeeded"
    forged, _ = webhook_request(row, event_id="evt_forged")
    _, attacker_headers = webhook_request(row, event_id="evt_forged", secret="whsec_attacker")
    assert post_webhook(forged, attacker_headers).status_code == 400
    _, good_headers = webhook_request(row, event_id="evt_x")
    tampered = forged.replace(b'"evt_forged"', b'"evt_x"')[:-1] + b" }"
    assert post_webhook(tampered, good_headers).status_code == 400
    assert post_webhook(forged, {}).status_code == 400  # unsigned
    payment.refresh_from_db()
    assert payment.status == P.PROCESSING and not WebhookEvent.objects.exists()


def test_contradicting_webhook_is_flagged_not_applied(provider):
    _, payment = completed_trip()
    charge_due_payments(provider=provider)  # succeeded synchronously
    row = SandboxCharge.objects.get()
    row.status, row.failure_code = "failed", "card_declined"
    response = post_webhook(*webhook_request(row, event_id="evt_late_failure"))
    assert response.json() == {"status": "conflict"}
    payment.refresh_from_db()
    assert payment.status == P.PAID and payment.needs_review  # never silently flipped


def test_webhook_with_wrong_amount_is_flagged():
    _, payment = completed_trip("tok_sandbox_async")
    charge_due_payments(provider=SandboxProvider())
    row = SandboxCharge.objects.get()
    row.status, row.amount = "succeeded", row.amount + 1
    assert post_webhook(*webhook_request(row)).json() == {"status": "conflict"}
    payment.refresh_from_db()
    assert payment.status == P.PROCESSING and payment.needs_review


def test_webhook_for_unknown_payment_is_acknowledged_and_ignored():
    row = SandboxCharge(reference="ch_unknown", idempotency_key="pay_x_charge_1", kind="charge",
                        status="succeeded", amount=1, currency="USD")  # fmt: skip
    assert post_webhook(*webhook_request(row)).json() == {"status": "ignored"}


# --- refunds --------------------------------------------------------------------------------------
def test_refund_is_idempotent_and_staff_only(provider):
    trip, payment = completed_trip()
    charge_due_payments(provider=provider)
    url = reverse("v1:payment-refund", kwargs={"payment_id": payment.pk})
    assert client_for(trip.rider).post(url).status_code == 403
    staff = client_for(make_rider(is_staff=True))
    assert staff.post(url).json()["status"] == P.REFUNDED
    assert staff.post(url).json()["status"] == P.REFUNDED  # repeat: no second refund
    assert SandboxCharge.objects.filter(kind="refund").count() == 1
    assert statuses(payment) == [P.PROCESSING, P.PAID, P.REFUNDED]


def test_unpaid_payment_cannot_be_refunded():
    _, payment = completed_trip()
    with pytest.raises(Exception, match="Only a paid"):
        refund_payment(payment_id=payment.pk, provider=SandboxProvider())


# --- API: payment methods never touch card data ---------------------------------------------------
def test_card_numbers_are_refused_and_never_echoed(rider_client, caplog):
    url = reverse("v1:payment-methods")
    for raw in ["4242424242424242", "4242 4242 4242 4242", "4242-4242-4242-4242"]:
        response = rider_client.post(url, {"provider_token": raw}, format="json")
        assert response.status_code == 400 and raw not in response.content.decode()
    response = rider_client.post(url, {"provider_token": "tok_sandbox_visa", "cvc": "123"},
                                 format="json")  # fmt: skip
    assert response.status_code == 400 and "cvc" in response.json()["error"]["details"]
    assert "4242424242424242" not in caplog.text


def test_tokens_are_stored_but_never_returned(rider_client):
    url = reverse("v1:payment-methods")
    added = rider_client.post(url, {"provider_token": "tok_sandbox_visa"}, format="json")
    assert added.status_code == 201
    content = added.content.decode()
    assert added.json()["last4"] == "4242" and "tok_sandbox" not in content
    assert "pm_sandbox" not in content
    listed = rider_client.get(url)
    assert "pm_sandbox" not in listed.content.decode() and listed.json()[0]["is_default"]
    # The same test card can be attached by another rider (each gets its own method id).
    other = client_for(make_rider()).post(url, {"provider_token": "tok_sandbox_visa"},
                                          format="json")  # fmt: skip
    assert other.status_code == 201
    bad = rider_client.post(url, {"provider_token": "tok_unknown"}, format="json")
    assert bad.status_code == 400


def test_rider_sees_own_payment_without_provider_internals(provider):
    trip, payment = completed_trip()
    charge_due_payments(provider=provider)
    url = reverse("v1:trip-payment", kwargs={"trip_id": trip.pk})
    body = client_for(trip.rider).get(url).json()
    assert body["status"] == P.PAID and body["card"] == {"brand": "visa", "last4": "4242"}
    assert "provider_reference" not in body and "needs_review" not in body
    assert client_for(make_rider()).get(url).status_code == 404
