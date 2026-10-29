"""Per-request / per-message correlation context (contextvars: safe for threads,
asyncio tasks and the Kafka consumer loop).

The id comes from the caller's `X-Request-ID` (validated, so log injection and
unbounded values are impossible) or is generated, is returned in the response
header, attached to every log line, stored on outbox rows and forwarded as a
Kafka header, so one id follows a ride request through API -> outbox -> Kafka ->
consumers.
"""

import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
VALID_ID = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")


def get_request_id() -> str | None:
    return _request_id.get()


def new_request_id() -> str:
    return uuid.uuid4().hex


def clean_request_id(value: str | None) -> str:
    return value if value and VALID_ID.match(value) else new_request_id()


@contextmanager
def bound_request_id(value: str | None):
    token = _request_id.set(clean_request_id(value))
    try:
        yield _request_id.get()
    finally:
        _request_id.reset(token)
