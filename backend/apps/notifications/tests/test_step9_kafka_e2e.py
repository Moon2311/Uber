"""End-to-end through a real Kafka broker: quote -> trip -> completion -> card
charge -> outbox -> Kafka -> `notifications` and `fraud` consumer groups."""

import pytest
from django.urls import reverse

from apps.events.consumer import ConsumerRunner
from apps.events.kafka import get_publisher
from apps.events.registry import group_config, physical_group
from apps.events.relay import OutboxRelay
from apps.fraud.models import RiskSignal, Subject
from apps.notifications.models import DeviceToken, Notification
from apps.payments.models import PaymentRecord, PaymentStatus
from apps.payments.providers.sandbox import SandboxProvider
from apps.payments.services import charge_due_payments
from apps.payments.tests.conftest import card
from apps.trips.models import Trip
from apps.trips.tests.conftest import S, advance
from conftest import client_for, make_driver, make_rider

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("kafka_topics")]
BODY = {"pickup": {"lat": "37.7749", "lng": "-122.4194"},
        "dropoff": {"lat": "37.8044", "lng": "-122.2712"}, "ride_category": "ECONOMY"}  # fmt: skip


def run_group(name):
    topics, handlers = group_config(name)
    ConsumerRunner(physical_group(name), topics, handlers).run(idle_timeout=6.0)


def test_paid_and_declined_trips_flow_to_notifications_and_fraud():
    outcomes = {}
    for token in ("tok_sandbox_visa", "tok_sandbox_decline"):
        rider, driver = make_rider(), make_driver(online=True)
        card(rider, token)
        DeviceToken.objects.create(user=rider, token=f"dev_ok_{rider.pk}", platform="IOS")
        client = client_for(rider)
        quote = client.post(reverse("v1:fare-estimate"), BODY, format="json").json()
        trip_id = client.post(reverse("v1:trip-create"), {**BODY, "quote_id": quote["quote_id"]},
                              format="json").json()["id"]  # fmt: skip
        advance(Trip.objects.get(pk=trip_id), S.SEARCHING, S.DRIVER_ASSIGNED, S.DRIVER_ARRIVING,
                S.IN_PROGRESS, S.COMPLETED, driver=driver)  # fmt: skip
        outcomes[token] = rider
    charge_due_payments(provider=SandboxProvider())
    assert set(PaymentRecord.objects.values_list("status", flat=True)) == {
        PaymentStatus.PAID,
        PaymentStatus.FAILED,
    }

    relay = OutboxRelay(get_publisher())
    while relay.run_once().published:
        pass
    run_group("notifications")
    run_group("fraud")

    paid, declined = outcomes["tok_sandbox_visa"], outcomes["tok_sandbox_decline"]
    assert set(Notification.objects.filter(user=paid).values_list("kind", flat=True)) == {
        "driver_assigned",
        "driver_arrived",
        "payment_paid",
    }
    assert "payment_failed" in Notification.objects.filter(user=declined).values_list(
        "kind", flat=True
    )
    riders = RiskSignal.objects.filter(subject_type=Subject.RIDER)
    [signal] = riders.filter(subject_id=declined.pk)
    assert signal.signal == "payment_failed" and signal.evidence["failure_code"] == "card_declined"
    assert not riders.filter(subject_id=paid.pk).exists()
    # The test completes trips milliseconds after starting them: correctly flagged.
    drivers = RiskSignal.objects.filter(subject_type=Subject.DRIVER)
    assert set(drivers.values_list("signal", flat=True)) == {"implausible_trip_speed"}
