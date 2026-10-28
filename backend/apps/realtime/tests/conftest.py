import asyncio

import pytest
from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.db import connection
from rest_framework_simplejwt.tokens import AccessToken

ORIGIN = "http://localhost:5173"


@pytest.fixture(autouse=True)
def keep_test_connection(monkeypatch):
    """channels' database_sync_to_async calls close_old_connections() around each DB
    call; inside the (never committed) test transaction that would close the test's
    connection. All DB work runs on the test thread here, so skip it in tests."""
    monkeypatch.setattr("channels.db.close_old_connections", lambda: None)


@pytest.fixture(autouse=True)
def realtime_settings(settings):
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    settings.REALTIME_ALLOWED_ORIGINS = [ORIGIN]
    settings.REALTIME_ALLOW_MISSING_ORIGIN = True
    settings.REALTIME_HEARTBEAT_INTERVAL_SECONDS = 25
    settings.REALTIME_HEARTBEAT_TIMEOUT_SECONDS = 60
    return settings


def run(scenario):
    """Run an async scenario on one event loop (the communicator's lifetime)."""
    return async_to_sync(scenario)()


def _commit_hooks():
    """The test transaction never commits, so transaction.on_commit callbacks (the
    notifier's pushes) would never fire. Run the pending ones, as a COMMIT would."""
    pending, connection.run_on_commit = connection.run_on_commit, []
    for _savepoints, func, _robust in pending:
        func()


commit = database_sync_to_async(_commit_hooks)
db = database_sync_to_async


class Client:
    def __init__(self, user=None, *, token=None, origin=ORIGIN, subprotocols=None):
        from config.asgi import application

        headers = [(b"origin", origin.encode())] if origin else []
        if subprotocols is None:
            token = token or str(AccessToken.for_user(user))
            subprotocols = ["jwt", token]
        self.c = WebsocketCommunicator(
            application, "/ws/v1/", headers=headers, subprotocols=subprotocols
        )
        self.welcome = None
        self.buffer = []  # messages skipped by recv(type_) wait here

    async def connect(self, expect=True):
        ok, sub = await self.c.connect(timeout=5)
        if expect:
            assert ok, f"connection rejected: {sub}"
            self.welcome = await self.recv("welcome")
            self.synced = await self.recv("synced")
        return ok, sub

    async def send(self, **message):
        await self.c.send_json_to(message)

    async def recv(self, type_=None, timeout=3):
        """Next message (of `type_` if given; skipped messages are buffered)."""
        for i, msg in enumerate(self.buffer):
            if type_ is None or msg["type"] == type_:
                return self.buffer.pop(i)
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            left = max(0.01, deadline - asyncio.get_event_loop().time())
            msg = await self.c.receive_json_from(timeout=left)
            if type_ is None or msg["type"] == type_:
                return msg
            self.buffer.append(msg)

    async def drain(self, timeout=0.3) -> list[dict]:
        """Everything that arrives within `timeout` (plus the buffer)."""
        out, self.buffer = self.buffer, []
        # receive_nothing() waits without cancelling the app (receive_output's timeout
        # would cancel the consumer).
        while not await self.c.receive_nothing(timeout=timeout):
            out.append(await self.c.receive_json_from(timeout=1))
        return out

    async def quiet(self, timeout=0.3):
        return not self.buffer and await self.c.receive_nothing(timeout=timeout)

    async def close(self):
        await self.c.disconnect()


@pytest.fixture
def live(settings, redis_client):
    """Matching stack as in production: Redis live index + Redis reservation locks."""
    settings.LOCATION_STORE = "redis"
    settings.LOCATION_STORE_FALLBACK = "none"
    settings.MATCHING_RESERVATION_LOCKS = "redis"
    settings.ETA_PROVIDER = "fallback"
    return settings
