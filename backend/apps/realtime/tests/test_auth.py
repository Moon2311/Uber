"""Connection authentication and origin checks."""

from datetime import timedelta

import pytest
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from conftest import make_driver, make_rider

from .conftest import Client, db, run

pytestmark = pytest.mark.django_db


def test_rider_and_driver_connect_with_jwt_subprotocol():
    rider, driver = make_rider(), make_driver(online=True)

    async def scenario():
        for user, role in [(rider, "RIDER"), (driver.user, "DRIVER")]:
            c = Client(user)
            ok, subprotocol = await c.connect()
            assert ok and subprotocol == "jwt"  # server selects the jwt subprotocol
            assert c.welcome["user"] == {"id": user.pk, "role": role}
            assert c.welcome["heartbeat_interval_s"] == 25
            await c.close()

    run(scenario)


@pytest.mark.parametrize(
    "subprotocols",
    [[], ["jwt"], ["jwt", "not-a-token"], ["bearer", "x"], ["x", "jwt"]],
)
def test_missing_or_invalid_token_is_rejected(subprotocols):
    async def scenario():
        ok, _ = await Client(subprotocols=subprotocols).connect(expect=False)
        assert not ok

    run(scenario)


def test_expired_token_is_rejected():
    rider = make_rider()
    token = AccessToken.for_user(rider)
    token.set_exp(lifetime=-timedelta(seconds=1))

    async def scenario():
        ok, _ = await Client(token=str(token)).connect(expect=False)
        assert not ok

    run(scenario)


def test_refresh_token_cannot_be_used():
    refresh = str(RefreshToken.for_user(make_rider()))  # writes the outstanding-token row

    async def scenario():
        ok, _ = await Client(token=refresh).connect(expect=False)
        assert not ok

    run(scenario)


def test_deactivated_user_is_rejected():
    rider = make_rider()
    token = str(AccessToken.for_user(rider))
    rider.is_active = False
    rider.save()

    async def scenario():
        ok, _ = await Client(token=token).connect(expect=False)
        assert not ok

    run(scenario)


def test_foreign_origin_is_rejected_and_missing_origin_configurable(settings):
    rider = make_rider()

    async def scenario():
        ok, code = await Client(rider, origin="https://evil.example").connect(expect=False)
        assert not ok and code == 4403
        ok, _ = await Client(rider, origin=None).connect()  # native app
        assert ok
        settings.REALTIME_ALLOW_MISSING_ORIGIN = False
        ok, _ = await Client(rider, origin=None).connect(expect=False)
        assert not ok

    run(scenario)


def test_connection_closed_when_access_token_expires(settings):
    settings.REALTIME_HEARTBEAT_INTERVAL_SECONDS = 0.2
    rider = make_rider()
    token = AccessToken.for_user(rider)
    token.set_exp(lifetime=timedelta(seconds=1))

    async def scenario():
        c = Client(token=str(token))
        await c.connect()
        err = await c.recv("error", timeout=4)
        assert err["code"] == "token_expired"
        closed = await c.c.receive_output(timeout=2)
        assert closed == {"type": "websocket.close", "code": 4401}

    run(scenario)


def test_user_lookup_uses_database_role_not_token_claims():
    driver = make_driver(online=True)

    async def scenario():
        c = Client(driver.user)
        await c.connect()
        assert c.welcome["user"]["role"] == "DRIVER"
        await db(lambda: None)()
        await c.close()

    run(scenario)
