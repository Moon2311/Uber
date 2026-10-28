"""Push notifications: which events alert whom, idempotent creation, retries,
invalid tokens, exhaustion, expiry and lease-based crash recovery."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.events import builders
from apps.events.consumer import ConsumedMessage, EventProcessor, Outcome
from apps.events.schemas import make_event
from apps.events.tests.fakes import FakePublisher
from apps.notifications.models import (
    DeliveryStatus,
    DeviceToken,
    Notification,
    NotificationStatus,
)
from apps.notifications.providers import SandboxPushProvider
from apps.notifications.services import HANDLERS, dispatch_due
from apps.trips.tests.conftest import S, advance, new_trip
from conftest import client_for, make_driver, make_rider

pytestmark = pytest.mark.django_db
NS = NotificationStatus


def consume(*events):
    processor = EventProcessor("notif-test", HANDLERS, FakePublisher(), sleep=lambda s: None)
    return [processor.process(ConsumedMessage("t", 0, i, "k", e.to_bytes()))
            for i, e in enumerate(events)]  # fmt: skip


def device(user, token="dev_ok_1", platform="IOS"):
    return DeviceToken.objects.create(user=user, token=token, platform=platform)


def payment_event(rider, to_status="FAILED"):
    return make_event("PaymentStatusChanged", {
        "payment_id": uuid.uuid4(), "trip_id": uuid.uuid4(), "rider_id": rider.pk,
        "from_status": "PROCESSING", "to_status": to_status, "amount": Decimal("12.00"),
        "currency": "USD", "method": "CARD", "failure_code": "card_declined",
        "changed_at": timezone.now(),
    })  # fmt: skip


def soon(seconds=3600):
    return timezone.now() + timedelta(seconds=seconds)


@pytest.fixture
def provider():
    SandboxPushProvider.sent = []
    return SandboxPushProvider()


# --- what gets pushed ------------------------------------------------------------------------
def test_important_trip_milestones_are_pushed_live_ones_are_not():
    driver = make_driver(online=True)
    trip = new_trip()
    advance(trip, S.SEARCHING, S.DRIVER_ASSIGNED, S.DRIVER_ARRIVING, S.IN_PROGRESS, driver=driver)
    events = [builders.trip_status_changed(trip, a, b) for a, b in [
        ("REQUESTED", "SEARCHING"), ("SEARCHING", "DRIVER_ASSIGNED"),
        ("DRIVER_ASSIGNED", "DRIVER_ARRIVING"), ("DRIVER_ARRIVING", "IN_PROGRESS"),
    ]]  # fmt: skip
    consume(*events)
    kinds = list(Notification.objects.order_by("created_at").values_list("kind", flat=True))
    assert kinds == ["driver_assigned", "driver_arrived"]  # searching / started: WebSocket only
    n = Notification.objects.first()
    assert n.user == trip.rider and n.data == {"trip_id": str(trip.pk)}
    assert trip.rider.first_name not in n.body  # no personal data in push payloads


def test_offers_alert_the_driver_and_payment_failures_alert_the_rider():
    driver, rider = make_driver(online=True), make_rider()
    offer = builders.ride_offer_created(trip_id=uuid.uuid4(), offer_id=uuid.uuid4(),
                                        driver_id=driver.pk, expires_at=soon(15),
                                        created_at=timezone.now())  # fmt: skip
    consume(offer, payment_event(rider, "FAILED"), payment_event(rider, "PROCESSING"))
    assert dict(Notification.objects.values_list("kind", "user_id")) == {
        "ride_offer": driver.user_id,
        "payment_failed": rider.pk,
    }


def test_redelivered_event_notifies_once():
    rider = make_rider()
    event = payment_event(rider)
    assert consume(event, event) == [Outcome.HANDLED, Outcome.DUPLICATE]
    # Even past the consumer ledger (e.g. it was pruned), the dedupe key holds.
    from apps.notifications.services import create_from_event

    assert create_from_event(event) == []
    assert Notification.objects.count() == 1


# --- delivery --------------------------------------------------------------------------------
def test_delivery_to_every_active_device(provider):
    rider = make_rider()
    a, b = device(rider, "dev_ok_a"), device(rider, "dev_ok_b", "ANDROID")
    device(rider, "dev_ok_old").delete()
    consume(payment_event(rider))
    assert dispatch_due(provider=provider) == 1
    n = Notification.objects.get()
    assert n.status == NS.SENT and n.sent_at and n.attempts == 1
    assert {d.device_id for d in n.deliveries.filter(status=DeliveryStatus.SENT)} == {a.pk, b.pk}
    assert len(provider.sent) == 2 and provider.sent[0]["data"]["notification_id"] == str(n.pk)


def test_transient_failure_retries_with_backoff_and_does_not_resend_to_others(provider):
    rider = make_rider()
    ok, flaky = device(rider, "dev_ok_a"), device(rider, "dev_transient_b")
    consume(payment_event(rider))
    dispatch_due(provider=provider)
    n = Notification.objects.get()
    assert n.status == NS.PENDING and n.next_attempt_at > timezone.now() and n.last_error
    assert len(provider.sent) == 1

    DeviceToken.objects.filter(pk=flaky.pk).update(token="dev_ok_b")  # push service recovered
    dispatch_due(provider=provider, now=soon())
    n.refresh_from_db()
    assert n.status == NS.SENT and n.attempts == 2
    assert [m["token"] for m in provider.sent] == ["dev_ok_a", "dev_ok_b"]  # ok not re-sent
    assert n.deliveries.get(device=ok).attempts == 1


def test_gives_up_after_max_attempts(provider, settings):
    settings.NOTIFICATIONS_MAX_ATTEMPTS = 3
    rider = make_rider()
    device(rider, "dev_transient_only")
    consume(payment_event(rider))
    for i in range(5):
        dispatch_due(provider=provider, now=soon(1000 * (i + 1)))
    n = Notification.objects.get()
    assert n.status == NS.FAILED and n.attempts == 3 and "unavailable" in n.last_error


def test_invalid_token_deactivates_the_device(provider):
    rider = make_rider()
    dead, alive = device(rider, "dev_invalid_x"), device(rider, "dev_ok_y")
    consume(payment_event(rider))
    dispatch_due(provider=provider)
    dead.refresh_from_db()
    assert not dead.is_active
    assert Notification.objects.get().status == NS.SENT
    assert dead.deliveries.get().status == DeliveryStatus.INVALID_TOKEN and alive.is_active


def test_no_devices_or_expired_alerts_are_skipped(provider):
    rider, driver = make_rider(), make_driver(online=True)
    consume(payment_event(rider))  # rider has no devices
    device(driver.user, "dev_ok_driver")
    consume(builders.ride_offer_created(trip_id=uuid.uuid4(), offer_id=uuid.uuid4(),
                                        driver_id=driver.pk, expires_at=soon(15),
                                        created_at=timezone.now()))  # fmt: skip
    dispatch_due(provider=provider, now=soon(120))  # the 60 s offer alert is stale by now
    assert dict(Notification.objects.values_list("kind", "last_error")) == {
        "payment_failed": "no_devices",
        "ride_offer": "expired",
    }
    assert set(Notification.objects.values_list("status", flat=True)) == {NS.SKIPPED}
    assert provider.sent == []


def test_rejected_payload_fails_without_retry(provider):
    rider = make_rider()
    device(rider, "dev_reject_payload")
    consume(payment_event(rider))
    dispatch_due(provider=provider)
    n = Notification.objects.get()
    assert n.status == NS.FAILED and n.attempts == 1


def test_worker_crash_after_claim_is_retried_after_the_lease(provider, monkeypatch):
    rider = make_rider()
    device(rider)
    consume(payment_event(rider))

    def crash(*a, **kw):
        raise SystemExit("worker killed")

    monkeypatch.setattr("apps.notifications.services.deliver", crash)
    with pytest.raises(SystemExit):
        dispatch_due(provider=provider)
    monkeypatch.undo()
    assert dispatch_due(provider=provider) == 0  # leased: not picked up immediately
    assert dispatch_due(provider=provider, now=soon(120)) == 1
    assert Notification.objects.get().status == NS.SENT


# --- API ----------------------------------------------------------------------------------------
def test_device_registration_and_inbox(rider_client, rider):
    url = reverse("v1:devices")
    first = rider_client.post(url, {"platform": "IOS", "token": "dev_ok_reg"}, format="json")
    assert first.status_code == 201 and "token" not in first.json()
    other = make_rider()
    client_for(other).post(url, {"platform": "IOS", "token": "dev_ok_reg"}, format="json")
    assert DeviceToken.objects.get(token="dev_ok_reg").user == other  # token moved with the phone
    consume(payment_event(rider))
    inbox = rider_client.get(reverse("v1:notifications")).json()
    rows = inbox["results"] if isinstance(inbox, dict) else inbox
    assert [r["kind"] for r in rows] == ["payment_failed"] and rows[0]["status"] == "PENDING"
