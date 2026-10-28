"""Subscription authorization and inbound message validation."""

import uuid

import pytest

from apps.matching.tests.conftest import driver_at, new_trip, north, orchestrator
from apps.trips.models import Trip, TripStatus
from conftest import make_driver, make_rider

from .conftest import Client, db, run

pytestmark = pytest.mark.django_db


def test_rider_can_subscribe_to_own_trip_only():
    owner, stranger = make_rider(), make_rider()
    trip = new_trip(rider=owner)

    async def scenario():
        mine = Client(owner)
        await mine.connect()
        assert mine.synced["trips"] == [str(trip.pk)]  # auto-joined on connect (state transfer)
        await mine.send(type="subscribe.trip", id="s1", trip_id=str(trip.pk))
        assert (await mine.recv("subscribed"))["reply_to"] == "s1"
        snapshot = (await mine.recv("trip.snapshot"))["data"]
        assert snapshot["id"] == str(trip.pk) and snapshot["version"]

        other = Client(stranger)
        await other.connect()
        await other.send(type="subscribe.trip", id="s2", trip_id=str(trip.pk))
        err = await other.recv("error")
        assert err["code"] == "not_found" and err["reply_to"] == "s2"
        await other.send(type="subscribe.trip", trip_id=str(uuid.uuid4()))
        assert (await other.recv("error"))["code"] == "not_found"  # same answer: no leak
        await mine.close()
        await other.close()

    run(scenario)


def test_driver_only_sees_trips_assigned_to_them(live):
    offered = driver_at(*north(0.2))
    assigned = make_driver(online=True)
    trip_offered = new_trip()
    orchestrator().run_once()  # `offered` now holds an offer for trip_offered
    trip_assigned = new_trip()
    Trip.objects.filter(pk=trip_assigned.pk).update(
        driver=assigned, status=TripStatus.DRIVER_ASSIGNED
    )

    async def scenario():
        c = Client(offered.user)
        await c.connect()
        await c.send(type="subscribe.trip", trip_id=str(trip_offered.pk))
        assert (await c.recv("error"))["code"] == "not_found"  # offer != access to rider data
        d = Client(assigned.user)
        await d.connect()
        assert d.synced["trips"] == [str(trip_assigned.pk)]  # auto-joined on connect
        await d.send(type="subscribe.trip", trip_id=str(trip_assigned.pk))
        assert (await d.recv("subscribed"))["trip_id"] == str(trip_assigned.pk)
        await c.close()
        await d.close()

    run(scenario)


@pytest.mark.parametrize(
    "message, code",
    [
        ({"type": "subscribe.trip", "trip_id": "../../etc"}, "invalid_trip_id"),
        ({"type": "subscribe.trip"}, "invalid_trip_id"),
        ({"type": "trip.status", "status": "DRIVER_ASSIGNED"}, "unknown_type"),  # injection
        ({"type": "push", "event": "offer.created"}, "unknown_type"),
        ({"type": "group.join", "group": "user.1"}, "unknown_type"),
        ({"nope": 1}, "unknown_type"),
    ],
)
def test_bad_messages_get_errors_and_change_nothing(message, code):
    rider = make_rider()

    async def scenario():
        c = Client(rider)
        await c.connect()
        await c.send(**message)
        assert (await c.recv("error"))["code"] == code
        await c.send(type="ping")
        await c.recv("pong")  # still connected
        await c.close()

    run(scenario)


def test_invalid_json_and_non_object():
    rider = make_rider()

    async def scenario():
        c = Client(rider)
        await c.connect()
        await c.c.send_to(text_data="{not json")
        assert (await c.recv("error"))["code"] == "invalid_json"
        await c.c.send_to(text_data="[1, 2]")
        assert (await c.recv("error"))["code"] == "invalid_message"
        await c.close()

    run(scenario)


def test_oversized_message_closes_connection(settings):
    settings.REALTIME_MAX_MESSAGE_BYTES = 256
    rider = make_rider()

    async def scenario():
        c = Client(rider)
        await c.connect()
        await c.send(type="ping", pad="x" * 1000)
        assert await c.c.receive_output(timeout=2) == {"type": "websocket.close", "code": 4413}

    run(scenario)


def test_riders_cannot_answer_offers():
    rider = make_rider()

    async def scenario():
        c = Client(rider)
        await c.connect()
        await c.send(type="offer.accept", offer_id=str(uuid.uuid4()))
        assert (await c.recv("error"))["code"] == "forbidden"
        await c.close()

    run(scenario)


def test_unauthorized_trip_never_receives_pushes():
    from apps.realtime import notifier
    from apps.realtime.groups import trip_group

    owner, stranger = make_rider(), make_rider()
    trip = new_trip(rider=owner)

    async def scenario():
        other = Client(stranger)
        await other.connect()
        await other.send(type="subscribe.trip", trip_id=str(trip.pk))
        await other.recv("error")
        await db(notifier.send)(trip_group(trip.pk), "trip.status", {"x": 1})
        assert await other.quiet()
        await other.close()

    run(scenario)
