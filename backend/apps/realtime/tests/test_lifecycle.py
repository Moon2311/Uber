"""Heartbeats, disconnects, reconnect state transfer, stale cleanup, multi-instance."""

import time

import pytest
from channels.layers import get_channel_layer

from apps.matching.tests.conftest import driver_at, new_trip, north, orchestrator
from apps.realtime import presence
from apps.realtime.groups import trip_group, user_group
from conftest import make_rider

from .conftest import Client, commit, db, run

pytestmark = pytest.mark.django_db


def test_ping_pong_keeps_connection_alive_and_silence_closes_it(settings):
    settings.REALTIME_HEARTBEAT_INTERVAL_SECONDS = 0.2
    settings.REALTIME_HEARTBEAT_TIMEOUT_SECONDS = 0.6
    rider = make_rider()

    async def scenario():
        import asyncio

        c = Client(rider)
        await c.connect()
        for _ in range(5):  # 1 s of regular pings > timeout
            await c.send(type="ping")
            await c.recv("pong")
            await asyncio.sleep(0.2)
        # now go silent
        closed = await c.c.receive_output(timeout=3)
        assert closed == {"type": "websocket.close", "code": 4408}

    run(scenario)


def test_disconnect_cleans_presence_and_groups(redis_client):
    rider = make_rider()

    async def scenario():
        c = Client(rider)
        await c.connect()
        conn_id = c.welcome["connection_id"]
        assert conn_id in await db(presence.connections)(rider.pk)
        await c.close()
        assert conn_id not in await db(presence.connections)(rider.pk)
        layer = get_channel_layer()
        assert not layer.groups.get(user_group(rider.pk))  # in-memory layer: group emptied

    run(scenario)


def test_stale_presence_cleanup(settings, redis_client):
    settings.REALTIME_HEARTBEAT_TIMEOUT_SECONDS = 60
    presence.touch(1, "alive")
    presence.touch(1, "crashed-instance")
    key = f"{settings.REDIS_KEY_PREFIX}:{{ws}}:presence:1"
    redis_client.hset(key, "crashed-instance", int(time.time()) - 600)
    assert presence.cleanup_stale() == 1
    assert set(presence.connections(1)) == {"alive"}


def test_driver_reconnect_gets_pending_offer_replayed(live):
    driver = driver_at(*north(0.2))
    new_trip()

    async def scenario():
        await db(lambda: orchestrator().run_once())()  # offer created while offline
        await commit()
        c = Client(driver.user)
        await c.connect()
        replay = await c.recv("offer.created")
        assert replay["replayed"] is True and c.synced["pending_offers"] == 1
        await c.send(type="offer.accept", offer_id=replay["data"]["id"])
        assert (await c.recv("offer.result"))["ok"] is True
        await c.close()

    run(scenario)


def test_rider_reconnect_converges_via_snapshot(live):
    driver = driver_at(*north(0.2))
    trip = new_trip()

    async def scenario():
        first = Client(trip.rider)
        await first.connect()
        await first.close()  # network drop
        # things happen while the rider is offline
        await db(lambda: orchestrator().run_once())()
        from apps.matching.models import RideOffer
        from apps.matching.offers import accept_offer
        from apps.matching.state import NullReservationLocks

        offer = await db(RideOffer.objects.get)(trip=trip)
        await db(accept_offer)(offer_id=offer.id, driver=driver, locks=NullReservationLocks())
        await commit()
        again = Client(trip.rider)
        await again.connect()
        snapshot = (await again.recv("trip.snapshot"))["data"]
        assert snapshot["status"] == "DRIVER_ASSIGNED" and snapshot["driver"]["id"]
        await again.send(type="sync", id="s")
        assert (await again.recv("synced"))["trips"] == [str(trip.pk)]
        await again.close()

    run(scenario)


def test_server_driven_join_when_rider_creates_trip_while_connected():
    rider = make_rider()

    async def scenario():
        c = Client(rider)
        await c.connect()
        trip = await db(new_trip)(rider=rider)
        await commit()
        snap = (await c.recv("trip.snapshot"))["data"]
        assert snap["id"] == str(trip.pk)
        from apps.realtime import notifier

        await db(notifier.send)(trip_group(trip.pk), "trip.status", {"probe": True})
        assert (await c.recv("trip.status"))["data"] == {"probe": True}
        await c.close()

    run(scenario)


def test_redis_channel_layer_delivers_across_instances(settings, redis_client):
    """Two app instances = two independent channel-layer clients sharing Redis. A
    group_send from instance B reaches a socket held by instance A."""
    from channels_redis.core import RedisChannelLayer

    prefix = f"{settings.REDIS_KEY_PREFIX}:ws:"
    config = {"hosts": ["redis://localhost:6380/15"], "prefix": prefix, "group_expiry": 300}
    settings.CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels_redis.core.RedisChannelLayer", "CONFIG": config}
    }
    rider = make_rider()
    trip = new_trip(rider=rider)

    async def scenario():
        c = Client(rider)  # served by "instance A" (the default layer)
        await c.connect()
        await c.recv("trip.snapshot")
        instance_b = RedisChannelLayer(**config)  # a different process's layer client
        await instance_b.group_send(
            trip_group(trip.pk), {"type": "push", "event": "trip.status", "data": {"via": "B"}}
        )
        assert (await c.recv("trip.status", timeout=5))["data"] == {"via": "B"}
        await c.close()

    run(scenario)


def test_inbound_flood_is_refused_then_disconnected(settings, recorded_metrics):
    settings.REALTIME_INBOUND_RATE_PER_SECOND = 1
    settings.REALTIME_INBOUND_BURST = 3
    settings.REALTIME_INBOUND_MAX_VIOLATIONS = 5
    rider = make_rider()

    async def scenario():
        c = Client(rider)
        await c.connect()
        for _ in range(3):  # within the burst
            await c.send(type="ping")
            await c.recv("pong")
        await c.send(type="ping")
        assert (await c.recv("error"))["code"] == "rate_limited"
        for _ in range(4):
            await c.send(type="ping")
        out = await c.c.receive_output(timeout=2)
        while out["type"] != "websocket.close":
            out = await c.c.receive_output(timeout=2)
        assert out["code"] == 4429

    run(scenario)
    assert recorded_metrics.count("realtime.rate_limited") == 5


def test_active_connection_gauge_tracks_open_sockets(recorded_metrics):
    rider = make_rider()
    key = ("realtime.connections", (("role", "RIDER"),))

    async def scenario():
        a, b = Client(rider), Client(rider)
        await a.connect()
        await b.connect()
        assert recorded_metrics.gauges[key] == 2
        await a.close()
        await b.close()

    run(scenario)
    assert recorded_metrics.gauges[key] == 0
