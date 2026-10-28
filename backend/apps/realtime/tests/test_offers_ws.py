"""Ride offers over WebSockets: routing to the selected driver, backend-validated
responses, expiry + rematch, concurrent acceptance, commit-only delivery."""

import asyncio
from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone

from apps.matching.models import MatchingJob, OfferStatus, RideOffer
from apps.matching.offers import expire_offers
from apps.matching.state import get_reservation_locks
from apps.matching.tests.conftest import driver_at, new_trip, north, orchestrator
from apps.trips import state_machine
from apps.trips.models import Trip, TripStatus

from .conftest import Client, commit, db, run

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("live")]


async def match():
    await db(lambda: orchestrator().run_once())()
    await commit()


def test_offer_goes_only_to_selected_driver_and_accept_assigns():
    chosen, other = driver_at(*north(0.2)), driver_at(*north(1.5))
    trip = new_trip()

    async def scenario():
        rider = Client(trip.rider)
        d1, d2 = Client(chosen.user), Client(other.user)
        for c in (rider, d1, d2):
            await c.connect()
        await match()
        offer = (await d1.recv("offer.created"))["data"]
        assert offer["trip"]["id"] == str(trip.pk) and offer["eta_is_estimate"] is True
        assert "rider" not in offer["trip"]  # no rider details before acceptance
        assert await d2.quiet()  # the other driver hears nothing
        assert (await rider.recv("trip.status"))["data"]["milestone"] == "searching"

        await d1.send(type="offer.accept", id="a1", offer_id=offer["id"])
        result = await d1.recv("offer.result")
        await commit()
        assert result["reply_to"] == "a1" and result["ok"] is True
        assert result["offer_status"] == "ACCEPTED" and result["trip_status"] == "DRIVER_ASSIGNED"
        assert result["assigned_to_me"] and result["trip"]["driver"]["id"]

        update = (await rider.recv("trip.status"))["data"]
        assert update["milestone"] == "driver_assigned"
        assert update["trip"]["driver"]["first_name"] == "Dan"
        assert update["trip"]["driver"]["vehicle"]["plate_number"]
        # authoritative state agrees
        db_trip = await db(Trip.objects.get)(pk=trip.pk)
        assert db_trip.driver_id == chosen.pk and db_trip.status == TripStatus.DRIVER_ASSIGNED
        for c in (rider, d1, d2):
            await c.close()

    run(scenario)


def test_decline_over_websocket_triggers_rematch_to_next_driver():
    first, second = driver_at(*north(0.2)), driver_at(*north(0.9))
    trip = new_trip()

    async def scenario():
        d1, d2 = Client(first.user), Client(second.user)
        await d1.connect()
        await d2.connect()
        await match()
        offer = (await d1.recv("offer.created"))["data"]
        await d1.send(type="offer.decline", offer_id=offer["id"])
        result = await d1.recv("offer.result")
        await commit()
        assert (
            result["ok"] and result["offer_status"] == "DECLINED" and not result["assigned_to_me"]
        )
        closed = (await d1.recv("offer.closed"))["data"]
        assert closed == {"offer_id": offer["id"], "status": "DECLINED", "reason": "declined"}
        await match()
        nxt = (await d2.recv("offer.created"))["data"]
        assert nxt["trip"]["id"] == str(trip.pk)
        await d1.close()
        await d2.close()

    run(scenario)


def test_unanswered_offer_expires_and_is_rematched_safely():
    first, second = driver_at(*north(0.2)), driver_at(*north(0.9))
    trip = new_trip()

    async def scenario():
        d1, d2 = Client(first.user), Client(second.user)
        await d1.connect()
        await d2.connect()
        await match()
        offer = (await d1.recv("offer.created"))["data"]
        await db(lambda: RideOffer.objects.filter(pk=offer["id"]).update(
            expires_at=timezone.now() - timedelta(seconds=1)))()  # fmt: skip
        assert await db(lambda: expire_offers(locks=get_reservation_locks()))() == 1
        await commit()
        closed = (await d1.recv("offer.closed"))["data"]
        assert closed["status"] == "EXPIRED" and closed["reason"] == "timeout"
        # late accept from the first driver is refused by the backend
        await d1.send(type="offer.accept", offer_id=offer["id"])
        late = await d1.recv("offer.result")
        assert late["ok"] is False and late["offer_status"] == "EXPIRED"
        assert late["trip_status"] == "SEARCHING" and not late["assigned_to_me"]
        await match()
        assert (await d2.recv("offer.created"))["data"]["trip"]["id"] == str(trip.pk)
        await d1.close()
        await d2.close()

    run(scenario)


def test_driver_cannot_answer_someone_elses_offer():
    owner, intruder = driver_at(*north(0.2)), driver_at(*north(5))
    new_trip()

    async def scenario():
        await match()
        offer = await db(lambda: RideOffer.objects.get(driver=owner))()
        c = Client(intruder.user)
        await c.connect()
        await c.send(type="offer.accept", offer_id=str(offer.id))
        result = await c.recv("offer.result")
        assert result["ok"] is False and result["error"] == "not_found"
        assert (await db(lambda: RideOffer.objects.get(pk=offer.id))()).status == "PENDING"
        await c.close()

    run(scenario)


def test_concurrent_accepts_from_two_devices_assign_once():
    driver = driver_at(*north(0.2))
    trip = new_trip()

    async def scenario():
        phone, tablet = Client(driver.user), Client(driver.user)
        await phone.connect()
        await tablet.connect()
        await match()
        offer_id = (await phone.recv("offer.created"))["data"]["id"]
        await tablet.recv("offer.created")
        await asyncio.gather(
            phone.send(type="offer.accept", offer_id=offer_id),
            tablet.send(type="offer.accept", offer_id=offer_id),
        )
        results = [await phone.recv("offer.result"), await tablet.recv("offer.result")]
        await commit()
        assert all(r["ok"] and r["offer_status"] == "ACCEPTED" for r in results)
        assert await db(lambda: Trip.objects.filter(driver=driver).count())() == 1
        assert (await db(Trip.objects.get)(pk=trip.pk)).status == "DRIVER_ASSIGNED"
        await phone.close()
        await tablet.close()

    run(scenario)


def test_rolled_back_assignment_is_never_pushed():
    """Pushes happen on COMMIT only: a WebSocket client can never be told about an
    assignment that the database did not keep."""
    driver = driver_at(*north(0.2))
    trip = new_trip()

    async def scenario():
        rider = Client(trip.rider)
        await rider.connect()
        await match()
        await rider.recv("trip.status")  # searching

        def assign_then_fail():
            try:
                with transaction.atomic():
                    t = Trip.objects.select_for_update().get(pk=trip.pk)
                    state_machine.transition(t, TripStatus.DRIVER_ASSIGNED, driver=driver)
                    raise RuntimeError("later step failed")
            except RuntimeError:
                pass

        await db(assign_then_fail)()
        await commit()
        everything = await rider.drain()
        # Only state-transfer snapshots (connect resync + server-driven join), all of
        # committed state: no status push and no driver anywhere.
        assert {m["type"] for m in everything} <= {"trip.snapshot"}, everything
        assert all(m["data"]["driver"] is None for m in everything)
        assert (await db(Trip.objects.get)(pk=trip.pk)).driver_id is None
        await rider.close()

    run(scenario)


def test_offer_cancelled_when_rider_cancels():
    driver = driver_at(*north(0.2))
    trip = new_trip()

    async def scenario():
        d = Client(driver.user)
        await d.connect()
        await match()
        offer = (await d.recv("offer.created"))["data"]
        from apps.trips.services import cancel_trip_by_rider

        await db(cancel_trip_by_rider)(trip_id=trip.pk, rider=trip.rider)
        await commit()
        closed = (await d.recv("offer.closed"))["data"]
        assert closed["offer_id"] == offer["id"] and closed["reason"] == "trip_cancelled"
        job = await db(MatchingJob.objects.get)(trip_id=trip.pk)
        assert job.status == "CANCELLED"
        await d.close()

    run(scenario)


def test_offer_status_in_reply_comes_from_database(monkeypatch):
    """Even if accept_offer were to 'succeed' without a DB change, the reply reports
    the stored state, not an optimistic one."""
    import apps.matching.offers as offers_module

    driver = driver_at(*north(0.2))
    new_trip()

    async def scenario():
        d = Client(driver.user)
        await d.connect()
        await match()
        offer_id = (await d.recv("offer.created"))["data"]["id"]
        monkeypatch.setattr(offers_module, "accept_offer", lambda **kw: None)  # no-op
        await d.send(type="offer.accept", offer_id=offer_id)
        result = await d.recv("offer.result")
        assert result["offer_status"] == OfferStatus.PENDING and not result["assigned_to_me"]
        await d.close()

    run(scenario)
