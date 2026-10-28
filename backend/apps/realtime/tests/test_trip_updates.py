"""Rider updates: assigned -> approaching -> arriving -> started -> completed,
and driver location fan-out restricted to the assigned trip."""

import pytest

from apps.matching.tests.conftest import PICKUP, driver_at, new_trip, north, orchestrator
from conftest import client_for

from .conftest import Client, commit, db, run

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("live")]


def post(user, path, body=None):
    return client_for(user).post(path, body or {}, format="json")


def test_rider_receives_every_milestone_and_driver_locations(settings):
    settings.REALTIME_APPROACHING_RADIUS_M = 300
    driver = driver_at(*north(1.0))
    bystander = driver_at(*north(3.0), category="XL")  # not involved
    trip = new_trip()

    async def scenario():
        rider, d, other = Client(trip.rider), Client(driver.user), Client(bystander.user)
        for c in (rider, d, other):
            await c.connect()
        await db(lambda: orchestrator().run_once())()
        await commit()
        offer = (await d.recv("offer.created"))["data"]
        await d.send(type="offer.accept", offer_id=offer["id"])
        await d.recv("offer.result")
        await commit()
        milestones = []
        milestones.append((await rider.recv("trip.status"))["data"]["milestone"])  # searching
        milestones.append((await rider.recv("trip.status"))["data"]["milestone"])  # assigned

        # driver drives toward the pickup: far ping, then within 300 m (twice)
        for km in (0.8, 0.25, 0.1):
            r = await db(post)(driver.user, "/api/v1/locations/update/",
                               {"lat": north(km)[0], "lng": PICKUP[1]})  # fmt: skip
            assert r.status_code == 200
            await commit()
        locs = [(await rider.recv("trip.driver_location"))["data"] for _ in range(3)]
        assert [round(loc["lat"], 4) for loc in locs] == [round(north(km)[0], 4)
                                                          for km in (0.8, 0.25, 0.1)]  # fmt: skip
        approaching = (await rider.recv("trip.driver_approaching"))["data"]
        assert approaching["distance_m"] <= 300

        for step in ("arrive", "start", "complete"):
            r = await db(post)(driver.user, f"/api/v1/trips/{trip.pk}/{step}/")
            assert r.status_code == 200
            await commit()
            milestones.append((await rider.recv("trip.status"))["data"]["milestone"])
        assert milestones == ["searching", "driver_assigned", "driver_arriving", "trip_started",
                              "trip_completed"]  # fmt: skip

        rest = await rider.drain()
        assert [m for m in rest if m["type"] == "trip.driver_approaching"] == []  # only once
        # after completion, location pings are no longer shared
        await db(post)(driver.user, "/api/v1/locations/update/",
                       {"lat": PICKUP[0], "lng": PICKUP[1]})  # fmt: skip
        await commit()
        assert [m for m in await rider.drain() if m["type"] == "trip.driver_location"] == []
        assert all(not m["type"].startswith("trip.") for m in await other.drain())
        for c in (rider, d, other):
            await c.close()

    run(scenario)


def test_unassigned_driver_locations_are_not_shared():
    driver = driver_at(*north(0.5))
    trip = new_trip()

    async def scenario():
        rider = Client(trip.rider)
        await rider.connect()
        await db(post)(driver.user, "/api/v1/locations/update/",
                       {"lat": north(0.4)[0], "lng": PICKUP[1]})  # fmt: skip
        await commit()
        assert [m for m in await rider.drain() if m["type"] == "trip.driver_location"] == []
        await rider.close()

    run(scenario)


def test_milestones_are_validated_by_backend():
    driver, stranger = driver_at(*north(0.5)), driver_at(*north(4))
    trip = new_trip()
    orchestrator().run_once()
    from apps.matching.models import RideOffer
    from apps.matching.offers import accept_offer
    from apps.matching.state import NullReservationLocks

    accept_offer(offer_id=RideOffer.objects.get(trip=trip).id, driver=driver,
                 locks=NullReservationLocks())  # fmt: skip
    assert post(stranger.user, f"/api/v1/trips/{trip.pk}/arrive/").status_code == 404
    assert post(trip.rider, f"/api/v1/trips/{trip.pk}/arrive/").status_code == 403
    assert post(driver.user, f"/api/v1/trips/{trip.pk}/start/").status_code == 409  # skip step
    assert post(driver.user, f"/api/v1/trips/{trip.pk}/arrive/").status_code == 200
    assert post(driver.user, f"/api/v1/trips/{trip.pk}/arrive/").status_code == 200  # idempotent
