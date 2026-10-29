"""Centralized lifecycle rules: legal edges, actors, compare-and-set, audit trail,
timestamps, outbox correlation, payment separation, and the HTTP commands."""

import itertools

import pytest
from django.db import transaction
from django.urls import reverse

from apps.events.models import OutboxEvent
from apps.matching.models import JobStatus, MatchingJob
from apps.payments.models import PaymentRecord, PaymentStatus
from apps.trips import state_machine
from apps.trips.models import ActorType, Trip, TripEvent
from apps.trips.services import (
    advance_trip_by_driver,
    cancel_trip_by_rider,
    release_trip_by_driver,
)
from apps.trips.state_machine import (
    TRANSITIONS,
    Actor,
    InvalidTransition,
    StaleTripState,
)
from conftest import client_for, make_driver, make_rider

from .conftest import S, advance, assert_audit_chain, new_trip, status_events

pytestmark = pytest.mark.django_db

HAPPY_PATH = [S.SEARCHING, S.DRIVER_ASSIGNED, S.DRIVER_ARRIVING, S.IN_PROGRESS, S.COMPLETED]


# --- the transition table ---------------------------------------------------------------
def test_required_lifecycle_is_legal_and_terminal_states_are_final():
    path = [S.REQUESTED, *HAPPY_PATH]
    for a, b in itertools.pairwise(path):
        assert (a, b) in TRANSITIONS
    for terminal in (S.COMPLETED, S.CANCELLED):
        assert not state_machine.ALLOWED_TRANSITIONS[terminal]
    # No skipping ahead, no going back (except the driver release edge).
    assert not state_machine.can_transition(S.REQUESTED, S.DRIVER_ASSIGNED)
    assert not state_machine.can_transition(S.SEARCHING, S.IN_PROGRESS)
    assert not state_machine.can_transition(S.IN_PROGRESS, S.CANCELLED)
    assert not state_machine.can_transition(S.IN_PROGRESS, S.SEARCHING)


@pytest.mark.parametrize(
    "current, target, actor, allowed",
    [
        (S.REQUESTED, S.SEARCHING, ActorType.SYSTEM, True),
        (S.REQUESTED, S.SEARCHING, ActorType.RIDER, False),
        (S.SEARCHING, S.DRIVER_ASSIGNED, ActorType.DRIVER, True),
        (S.SEARCHING, S.DRIVER_ASSIGNED, ActorType.SYSTEM, False),
        (S.DRIVER_ASSIGNED, S.DRIVER_ARRIVING, ActorType.RIDER, False),
        (S.DRIVER_ARRIVING, S.IN_PROGRESS, ActorType.DRIVER, True),
        (S.IN_PROGRESS, S.COMPLETED, ActorType.RIDER, False),
        (S.SEARCHING, S.CANCELLED, ActorType.RIDER, True),
        (S.DRIVER_ASSIGNED, S.CANCELLED, ActorType.DRIVER, False),  # drivers release instead
        (S.DRIVER_ASSIGNED, S.SEARCHING, ActorType.DRIVER, True),
        (S.DRIVER_ASSIGNED, S.SEARCHING, ActorType.RIDER, False),
    ],
)
def test_actor_rules(current, target, actor, allowed):
    assert state_machine.can_transition(current, target, actor) is allowed


def test_every_illegal_pair_is_rejected_and_leaves_no_trace():
    driver = make_driver(online=True)
    for status in S.values:
        for target in S.values:
            if (status, target) in TRANSITIONS:
                continue
            trip = Trip(status=status, state_version=0)
            with pytest.raises(InvalidTransition), transaction.atomic():
                state_machine.transition(trip, target, actor=Actor.driver(driver))
    assert not TripEvent.objects.exists()


def test_wrong_actor_is_rejected():
    trip = new_trip()
    with pytest.raises(InvalidTransition, match="Rider cannot"), transaction.atomic():
        state_machine.transition(trip, S.SEARCHING, actor=Actor.rider(trip.rider))
    trip.refresh_from_db()
    assert trip.status == S.REQUESTED and trip.state_version == 0


@pytest.mark.django_db(transaction=True)  # a real autocommit connection, no test wrapper
def test_transition_requires_a_transaction():
    trip = new_trip()
    with pytest.raises(RuntimeError, match="transaction"):
        state_machine.transition(trip, S.SEARCHING, actor=Actor.system())


def test_transition_cannot_touch_arbitrary_fields():
    trip = new_trip()
    with pytest.raises(ValueError, match="estimated_fare"), transaction.atomic():
        state_machine.transition(trip, S.SEARCHING, actor=Actor.system(), estimated_fare=0)


# --- optimistic concurrency ---------------------------------------------------------------
def test_stale_copy_loses_without_overwriting():
    trip = new_trip()
    copy_a = Trip.objects.get(pk=trip.pk)
    copy_b = Trip.objects.get(pk=trip.pk)
    with transaction.atomic():
        state_machine.transition(copy_a, S.CANCELLED, actor=Actor.rider(trip.rider), reason="a")
    # copy_b still believes REQUESTED/v0; the conditional UPDATE matches no row.
    with pytest.raises(StaleTripState), transaction.atomic():
        state_machine.transition(copy_b, S.SEARCHING, actor=Actor.system())
    trip.refresh_from_db()
    assert trip.status == S.CANCELLED and trip.state_version == 1
    assert_audit_chain(trip)


def test_expected_version_mismatch_is_stale():
    trip = new_trip()
    with pytest.raises(StaleTripState):
        cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider, expected_version=7)
    cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider, expected_version=0)
    assert Trip.objects.get(pk=trip.pk).status == S.CANCELLED


# --- audit trail and timestamps -------------------------------------------------------------
def test_full_lifecycle_records_every_step_with_actor_and_timestamps():
    driver = make_driver(online=True)
    trip = advance(new_trip(), *HAPPY_PATH, driver=driver)
    events = assert_audit_chain(trip)
    assert [e.to_status for e in events] == [S.REQUESTED, *HAPPY_PATH]
    assert [e.actor_type for e in events] == ["RIDER", "SYSTEM", "DRIVER", "DRIVER", "DRIVER",
                                              "DRIVER"]  # fmt: skip
    assert events[0].actor_id == trip.rider_id and events[2].actor_id == driver.pk
    stamps = [trip.created_at, trip.search_started_at, trip.driver_assigned_at,
              trip.driver_arrived_at, trip.started_at, trip.completed_at]  # fmt: skip
    assert all(stamps) and stamps == sorted(stamps)
    assert trip.state_version == 5 and trip.cancelled_at is None


def test_audit_event_ids_are_the_outbox_event_ids():
    driver = make_driver(online=True)
    trip = advance(new_trip(), S.SEARCHING, S.DRIVER_ASSIGNED, driver=driver)
    events = status_events(trip)
    outbox = {
        str(row.event_id): row
        for row in OutboxEvent.objects.filter(
            key=str(trip.pk), event_type__in=["RideRequested", "TripStatusChanged"]
        )
    }
    assert set(outbox) == {str(e.event_id) for e in events}
    assert outbox[str(events[2].event_id)].payload["to_status"] == S.DRIVER_ASSIGNED


def test_rollback_leaves_no_status_audit_or_outbox():
    trip = new_trip()
    before = (TripEvent.objects.count(), OutboxEvent.objects.count())
    with pytest.raises(RuntimeError), transaction.atomic():
        locked = Trip.objects.select_for_update().get(pk=trip.pk)
        state_machine.transition(locked, S.SEARCHING, actor=Actor.system())
        raise RuntimeError("crash after the transition, before commit")
    trip.refresh_from_db()
    assert trip.status == S.REQUESTED and trip.state_version == 0
    assert (TripEvent.objects.count(), OutboxEvent.objects.count()) == before


# --- cancellation ------------------------------------------------------------------------
def test_rider_cancel_is_recorded_and_idempotent():
    trip = new_trip()
    first = cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider, reason="changed my mind")
    again = cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider, reason="double tap")
    assert first.status == again.status == S.CANCELLED
    assert again.state_version == 1 and again.cancellation_reason == "changed my mind"
    assert again.cancelled_by == ActorType.RIDER and again.cancelled_at is not None
    events = assert_audit_chain(again)
    assert events[-1].reason == "changed my mind" and events[-1].actor_type == "RIDER"


def test_rider_cannot_cancel_a_trip_in_progress():
    driver = make_driver(online=True)
    trip = advance(new_trip(), S.SEARCHING, S.DRIVER_ASSIGNED, S.DRIVER_ARRIVING, S.IN_PROGRESS,
                   driver=driver)  # fmt: skip
    with pytest.raises(Exception, match="cannot be cancelled"):
        cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider)


# --- driver milestones and release ---------------------------------------------------------
def test_milestones_are_idempotent_and_ordered():
    driver = make_driver(online=True)
    trip = advance(new_trip(), S.SEARCHING, S.DRIVER_ASSIGNED, driver=driver)
    advance_trip_by_driver(trip_id=trip.pk, driver=driver, milestone="arrive")
    replay = advance_trip_by_driver(trip_id=trip.pk, driver=driver, milestone="arrive")
    assert replay.status == S.DRIVER_ARRIVING and replay.state_version == 3
    with pytest.raises(Exception, match="Cannot complete"):
        advance_trip_by_driver(trip_id=trip.pk, driver=driver, milestone="complete")
    other = make_driver(online=True)
    with pytest.raises(Trip.DoesNotExist):
        advance_trip_by_driver(trip_id=trip.pk, driver=other, milestone="start")
    assert_audit_chain(trip)


def test_driver_release_returns_trip_to_matching_without_that_driver():
    driver = make_driver(online=True)
    trip = advance(new_trip(), S.SEARCHING, S.DRIVER_ASSIGNED, S.DRIVER_ARRIVING, driver=driver)
    MatchingJob.objects.filter(trip=trip).update(status=JobStatus.MATCHED, offers_made=5)

    released = release_trip_by_driver(trip_id=trip.pk, driver=driver, reason="flat tyre")
    assert released.status == S.SEARCHING and released.driver is None
    assert released.driver_assigned_at is None and released.driver_arrived_at is None
    assert released.search_started_at is not None  # first-entry timestamp is kept
    job = MatchingJob.objects.get(trip=trip)
    assert job.status == JobStatus.ACTIVE and job.offers_made == 0
    last = assert_audit_chain(released)[-1]
    assert (last.from_status, last.actor_type, last.driver_id) == (S.DRIVER_ARRIVING, "DRIVER",
                                                                   driver.pk)  # fmt: skip
    # Retry is harmless; a stranger still gets 404.
    assert release_trip_by_driver(trip_id=trip.pk, driver=driver).state_version == 4
    with pytest.raises(Trip.DoesNotExist):
        release_trip_by_driver(trip_id=trip.pk, driver=make_driver(online=True))
    # The released trip can be assigned to someone else.
    advance(released, S.DRIVER_ASSIGNED, driver=make_driver(online=True))
    assert_audit_chain(released)


def test_release_after_pickup_is_refused():
    driver = make_driver(online=True)
    trip = advance(new_trip(), S.SEARCHING, S.DRIVER_ASSIGNED, S.DRIVER_ARRIVING, S.IN_PROGRESS,
                   driver=driver)  # fmt: skip
    with pytest.raises(Exception, match="cannot be released"):
        release_trip_by_driver(trip_id=trip.pk, driver=driver)


# --- payment status is a separate state machine ------------------------------------------------
def test_payment_follows_trip_but_trip_never_reads_payment():
    driver = make_driver(online=True)
    trip = new_trip()
    payment = PaymentRecord.objects.get(trip=trip)
    assert payment.status == PaymentStatus.PENDING
    # A payment failure does not move the trip...
    PaymentRecord.objects.filter(pk=payment.pk).update(status=PaymentStatus.FAILED)
    trip = advance(trip, *HAPPY_PATH, driver=driver)
    assert trip.status == S.COMPLETED
    # ...and completing the trip does not settle the payment.
    assert PaymentRecord.objects.get(pk=payment.pk).status == PaymentStatus.FAILED
    assert not hasattr(Trip, "payment_status")


def test_cancellation_cancels_only_an_unsettled_payment():
    trip = new_trip()
    PaymentRecord.objects.filter(trip=trip).update(status=PaymentStatus.PROCESSING)
    cancel_trip_by_rider(trip_id=trip.pk, rider=trip.rider)
    # An in-flight charge is settled by payments reconciliation, not by the trip.
    assert PaymentRecord.objects.get(trip=trip).status == PaymentStatus.PROCESSING


# --- HTTP ---------------------------------------------------------------------------------
def test_api_exposes_version_and_rejects_stale_commands(rider_client, rider):
    trip = new_trip(rider)
    url = reverse("v1:trip-detail", kwargs={"trip_id": trip.pk})
    assert rider_client.get(url).json()["state_version"] == 0
    cancel = reverse("v1:trip-cancel", kwargs={"trip_id": trip.pk})
    stale = rider_client.post(cancel, {"expected_version": 3}, format="json")
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_trip_state"
    ok = rider_client.post(cancel, {"expected_version": 0, "reason": "x"}, format="json")
    assert ok.status_code == 200
    body = ok.json()
    assert body["state_version"] == 1 and body["cancelled_by"] == "RIDER"


def test_api_driver_milestone_and_release(rider):
    driver = make_driver(online=True)
    trip = advance(new_trip(rider), S.SEARCHING, S.DRIVER_ASSIGNED, driver=driver)
    client = client_for(driver.user)
    arrive = reverse("v1:trip-arrive", kwargs={"trip_id": trip.pk})
    assert client.post(arrive, {"expected_version": 1}, format="json").status_code == 409
    assert client.post(arrive, {}, format="json").json()["status"] == S.DRIVER_ARRIVING
    release = reverse("v1:trip-release", kwargs={"trip_id": trip.pk})
    assert client_for(rider).post(release, {}).status_code == 403
    response = client.post(release, {"reason": "car trouble"}, format="json")
    assert response.status_code == 200 and response.json() == {"id": str(trip.pk),
                                                                "released": True}  # fmt: skip
    # Released driver can no longer read the trip.
    detail = reverse("v1:trip-detail", kwargs={"trip_id": trip.pk})
    assert client.get(detail).status_code == 404


def test_audit_endpoint_is_staff_only(rider_client, rider):
    trip = new_trip(rider)
    url = reverse("v1:trip-events", kwargs={"trip_id": trip.pk})
    assert rider_client.get(url).status_code == 403
    staff = make_rider(is_staff=True)
    body = client_for(staff).get(url).json()
    rows = body["results"] if isinstance(body, dict) else body
    assert [r["to_status"] for r in rows] == ["REQUESTED"] and rows[0]["state_version"] == 0
