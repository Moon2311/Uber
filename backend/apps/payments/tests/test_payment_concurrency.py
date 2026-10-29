"""Racing workers and racing duplicate callbacks, with real threads/connections."""

import pytest

from apps.payments.models import AttemptStatus, PaymentStatus, SandboxCharge, WebhookEvent
from apps.payments.providers.sandbox import SandboxProvider
from apps.payments.services import (
    charge_due_payments,
    charge_payment,
    process_webhook,
    reconcile_open_attempts,
)
from apps.trips.tests.test_trip_concurrency import in_threads

from .conftest import completed_trip
from .test_payments import later

pytestmark = pytest.mark.django_db(transaction=True)


def test_parallel_workers_charge_a_payment_once():
    _, payment = completed_trip()
    results = in_threads([lambda: charge_payment(payment.pk, provider=SandboxProvider())] * 5)
    assert all(kind == "ok" for kind, _ in results), results
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.PAID and payment.attempts.count() == 1
    assert SandboxCharge.objects.filter(kind="charge").count() == 1


def test_parallel_due_scans_split_the_work():
    payments = [completed_trip()[1] for _ in range(6)]
    results = in_threads([lambda: charge_due_payments(provider=SandboxProvider())] * 3)
    assert sum(value for _, value in results) == 6  # each payment claimed exactly once
    for p in payments:
        p.refresh_from_db()
        assert p.status == PaymentStatus.PAID and p.attempts.count() == 1


def test_duplicate_webhooks_racing_apply_once():
    _, payment = completed_trip("tok_sandbox_async")
    charge_due_payments(provider=SandboxProvider())
    ref = payment.attempts.get().provider_reference
    body, headers = SandboxProvider.settle_async(ref, "succeeded", event_id="evt_race")
    results = in_threads([lambda: process_webhook("sandbox", body, headers)] * 6)
    outcomes = sorted(value for _, value in results)
    assert outcomes.count("applied") == 1 and outcomes.count("duplicate") == 5, outcomes
    assert WebhookEvent.objects.count() == 1
    payment.refresh_from_db()
    assert payment.status == PaymentStatus.PAID


def test_webhook_and_reconciliation_racing_agree():
    _, payment = completed_trip("tok_sandbox_async")
    charge_due_payments(provider=SandboxProvider())
    ref = payment.attempts.get().provider_reference
    body, headers = SandboxProvider.settle_async(ref, "succeeded")
    in_threads([
        lambda: process_webhook("sandbox", body, headers),
        lambda: reconcile_open_attempts(provider=SandboxProvider(), now=later()),
    ])  # fmt: skip
    payment.refresh_from_db()
    attempt = payment.attempts.get()
    assert payment.status == PaymentStatus.PAID and attempt.status == AttemptStatus.SUCCEEDED
    assert not payment.needs_review
