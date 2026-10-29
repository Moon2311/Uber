"""Webhook signatures: HMAC-SHA256 over "<timestamp>.<raw body>".

Header:  Payment-Signature: t=<unix seconds>,v1=<hex>[,v1=<hex>...]

* Verified against the RAW bytes before any JSON parsing.
* Several v1 values / several configured secrets allow zero-downtime rotation.
* The timestamp must be within PAYMENTS_WEBHOOK_TOLERANCE_SECONDS (replay window);
  replays inside the window are stopped by event-id deduplication.
* No secret configured -> every webhook is rejected (fail closed).
"""

import hashlib
import hmac
import time

from django.conf import settings

from .providers.base import InvalidSignature

HEADER = "Payment-Signature"


def compute(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def sign(body: bytes, *, secret: str | None = None, timestamp: int | None = None) -> str:
    """Header value for `body` (used by the sandbox and by tests)."""
    secret = secret or settings.PAYMENTS_WEBHOOK_SECRETS[0]
    timestamp = int(time.time()) if timestamp is None else timestamp
    return f"t={timestamp},v1={compute(secret, timestamp, body)}"


def verify(body: bytes, header: str | None, *, now: float | None = None) -> None:
    secrets = [s for s in settings.PAYMENTS_WEBHOOK_SECRETS if s]
    if not secrets:
        raise InvalidSignature("no webhook secret configured")
    if not header:
        raise InvalidSignature("missing signature")
    timestamp, candidates = None, []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1" and value:
            candidates.append(value)
    if timestamp is None or not candidates:
        raise InvalidSignature("malformed signature header")
    now = time.time() if now is None else now
    if abs(now - timestamp) > settings.PAYMENTS_WEBHOOK_TOLERANCE_SECONDS:
        raise InvalidSignature("timestamp outside tolerance")
    for secret in secrets:
        expected = compute(secret, timestamp, body)
        if any(hmac.compare_digest(expected, c) for c in candidates):
            return
    raise InvalidSignature("signature mismatch")
