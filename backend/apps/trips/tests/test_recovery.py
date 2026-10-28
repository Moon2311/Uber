"""Partial failures and recovery.

* A failure anywhere inside a trip transaction (outbox, payments, matching,
  audit) rolls back ALL of it: there is no state in which the trip changed but
  its dependants did not.
* Kafka being down never blocks a trip change: events wait in the outbox and are
  published, in order, once the broker is back.
* reconcile_trips finds dependants that disagree with their trip (manual SQL,
  restores, bugs) and repairs what is safe, re-checking under the trip lock.
"""

import pytest
from django.core.management import call_command
from django.db import transaction

from apps.events import outbox
from apps.events.models import OutboxEvent, OutboxStatus
from apps.events.relay import OutboxRelay
from apps.events.schemas import decode
from apps.events.tests.fakes import FakePublisher
from apps.matching.models import JobStatus, MatchingJob, OfferStatus, RideOffer
from apps.payments import services as payment_services
from apps.payments.models import PaymentRecord, PaymentStatus
from apps.trips import reconciliation
from apps.trips.models import Trip, TripEvent
from apps.trips.reconciliation import reconcile
from apps.trips.services import cancel_trip_by_rider, request_trip
from conftest import make_driver, make_rider

from .conftest import DROPOFF, PICKUP, S, advance, assert_audit_chain, new_trip, pending_offer

pytestmark = pytest.mark.django_db


def snapshot(trip):
    trip.refresh_from_db()
    return (
        trip.status,
        trip.state_version,
        TripEvent.objects.filter(trip_id=trip.pk).count(),
        OutboxEvent.objects.filter(key=str(trip.pk)).count(),
        PaymentRecord.objects.get(trip=trip).status,
        MatchingJob.objects.get(trip=trip).status,
    )


# --- atomicity ---------------------------------------------------------------------------
def test_outbox_failure_rolls_back_the_cancellation(monkeypatch):
    trip = new_trip()
    before = snapshot(trip)

    def broken(event):
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr(outbox, "enqueue", broken)
    with pytest.raises(RuntimeError):
        cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider)
    assert snapshot(trip) == before  # trip, audit, payment, job: all untouched
    monkeypatch.undo()
    cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider)  # retry succeeds cleanly
    assert_audit_chain(trip)


def test_payment_failure_means_the_trip_was_never_created(monkeypatch):
    rider = make_rider()

    def broken(*, trip):
        raise RuntimeError("payments table unavailable")

    monkeypatch.setattr(payment_services, "create_pending_payment", broken)
    monkeypatch.setattr("apps.payments.receivers.create_pending_payment", broken)
    with pytest.raises(RuntimeError):
        request_trip(rider=rider, pickup=PICKUP, dropoff=DROPOFF, ride_category="ECONOMY")
    assert not Trip.objects.filter(rider=rider).exists()
    assert not TripEvent.objects.exists() and not OutboxEvent.objects.exists()
    assert not MatchingJob.objects.exists()


def test_kafka_outage_does_not_block_trips_and_events_drain_in_order():
    driver = make_driver(online=True)
    trip = new_trip()
    down = OutboxRelay(FakePublisher(fail_from=0))
    assert down.run_once().failed == 1  # broker unreachable
    advance(trip, S.SEARCHING, S.DRIVER_ASSIGNED, S.DRIVER_ARRIVING, driver=driver)
    assert trip.status == S.DRIVER_ARRIVING  # the business flow did not wait for Kafka

    OutboxEvent.objects.update(next_attempt_at=None)  # backoff elapsed
    up = FakePublisher()
    while OutboxRelay(up).run_once().published:
        pass
    assert not OutboxEvent.objects.filter(status=OutboxStatus.PENDING).exists()
    # Every audit step reached Kafka exactly once, in the order it happened.
    sent = [decode(m.value) for m in up.sent if m.key == str(trip.pk)]
    lifecycle = [e.event_id for e in sent if e.event_type in ("RideRequested",
                                                              "TripStatusChanged")]  # fmt: skip
    assert lifecycle == [str(e.event_id) for e in assert_audit_chain(trip)]


# --- reconciliation ------------------------------------------------------------------------
def test_clean_system_reports_nothing():
    driver = make_driver(online=True)
    advance(new_trip(), S.SEARCHING, S.DRIVER_ASSIGNED, driver=driver)
    trip = new_trip()
    cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider)
    assert reconcile().clean


def test_matchable_trip_without_job_gets_its_job_back():
    trip = new_trip()
    MatchingJob.objects.filter(trip=trip).update(status=JobStatus.EXHAUSTED)
    MatchingJob.objects.filter(trip=new_trip()).delete()
    report = reconcile(fix=True)
    assert len(report.found["matchable_without_active_job"]) == 2
    assert (
        report.repaired["matchable_without_active_job"]
        == report.found["matchable_without_active_job"]
    )
    assert MatchingJob.objects.filter(status=JobStatus.ACTIVE).count() == 2
    assert reconcile().clean


def test_closed_trip_with_active_job_and_pending_offer_is_cleaned_up():
    driver = make_driver(online=True)
    trip = advance(new_trip(), S.SEARCHING)
    pending_offer(trip, driver)
    # Simulate a write that bypassed the matching receivers (e.g. manual SQL + audit).
    with transaction.atomic():
        Trip.objects.filter(pk=trip.pk).update(status=S.CANCELLED, cancelled_by="ADMIN",
                                               state_version=2)  # fmt: skip
        TripEvent.objects.create(trip_id=trip.pk, event_type="STATUS_CHANGED", state_version=2,
                                 from_status=S.SEARCHING, to_status=S.CANCELLED,
                                 actor_type="ADMIN", occurred_at=trip.updated_at)  # fmt: skip
    report = reconcile(fix=True)
    assert report.found["active_job_for_closed_trip"] == [str(trip.pk)]
    assert report.found["pending_offer_for_non_searching_trip"] == [str(trip.pk)]
    assert report.found["payment_live_for_cancelled_trip"] == [str(trip.pk)]
    assert MatchingJob.objects.get(trip=trip).status == JobStatus.CANCELLED
    assert RideOffer.objects.get(trip=trip).status == OfferStatus.CANCELLED
    assert PaymentRecord.objects.get(trip=trip).status == PaymentStatus.CANCELLED
    assert reconcile().clean


def test_missing_payment_is_recreated_in_the_right_state():
    trip = new_trip()
    cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider)
    PaymentRecord.objects.filter(trip=trip).delete()
    report = reconcile(fix=True)
    assert report.repaired["missing_payment_record"] == [str(trip.pk)]
    assert PaymentRecord.objects.get(trip=trip).status == PaymentStatus.CANCELLED


def test_status_written_outside_the_state_machine_is_reported_not_rewritten():
    trip = new_trip()
    Trip.objects.filter(pk=trip.pk).update(status=S.SEARCHING)  # no audit, no version bump
    report = reconcile(fix=True)
    assert report.found["audit_chain_mismatch"] == [str(trip.pk)]
    assert not report.repaired.get("audit_chain_mismatch")  # never invent history
    assert TripEvent.objects.filter(trip_id=trip.pk).count() == 1


def test_repair_rechecks_under_the_lock():
    """A trip that changes between detection and repair is left alone."""
    trip = new_trip()
    MatchingJob.objects.filter(trip=trip).delete()
    cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider)  # happens "in between"
    assert reconciliation._repair_job(trip.pk, trip.updated_at) is False
    assert not MatchingJob.objects.filter(trip=trip).exists()


def test_failed_outbox_events_are_surfaced():
    trip = new_trip()
    OutboxEvent.objects.filter(key=str(trip.pk)).update(status=OutboxStatus.FAILED)
    assert reconcile().found["outbox_gave_up"] == [str(trip.pk)]


def test_command_exit_code_reflects_unrepaired_findings():
    trip = new_trip()
    call_command("reconcile_trips")  # clean -> exit 0
    Trip.objects.filter(pk=trip.pk).update(status=S.SEARCHING)
    with pytest.raises(SystemExit) as exc:
        call_command("reconcile_trips", "--fix")
    assert exc.value.code == 1
