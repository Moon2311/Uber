"""Push-provider abstraction (FCM / APNs / Web Push would implement `send`)."""

from __future__ import annotations

import logging
import secrets
from abc import ABC, abstractmethod

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

logger = logging.getLogger("apps.notifications")


class TransientDeliveryError(Exception):
    """Timeout, 5xx, rate limit: retry later."""


class InvalidTokenError(Exception):
    """The push service says this device token is gone: deactivate it."""


class PermanentDeliveryError(Exception):
    """The message itself is rejected (payload too large...): do not retry."""


class PushProvider(ABC):
    name: str

    @abstractmethod
    def send(self, *, token: str, platform: str, title: str, body: str, data: dict,
             collapse_key: str, idempotency_key: str) -> str:  # fmt: skip
        """Returns the provider message id or raises one of the errors above."""


class SandboxPushProvider(PushProvider):
    """Logs instead of sending. Tokens steer behaviour for tests and local dev:
    dev_transient_* -> TransientDeliveryError, dev_invalid_* -> InvalidTokenError,
    dev_reject_* -> PermanentDeliveryError, anything else -> accepted."""

    name = "sandbox"
    sent: list[dict] = []  # in-process record for local inspection / tests

    def send(self, *, token, platform, title, body, data, collapse_key, idempotency_key):
        if token.startswith("dev_transient"):
            raise TransientDeliveryError("sandbox: push service unavailable")
        if token.startswith("dev_invalid"):
            raise InvalidTokenError("sandbox: unregistered token")
        if token.startswith("dev_reject"):
            raise PermanentDeliveryError("sandbox: payload rejected")
        message_id = f"msg_{secrets.token_hex(6)}"
        self.sent.append({"token": token, "title": title, "data": data, "id": message_id,
                          "idempotency_key": idempotency_key})  # fmt: skip
        logger.info("notifications.sandbox.sent", extra={"platform": platform, "title": title})
        return message_id


PROVIDERS = {"sandbox": "apps.notifications.providers.SandboxPushProvider"}


def get_push_provider() -> PushProvider:
    name = settings.NOTIFICATIONS_PROVIDER
    if name not in PROVIDERS:
        raise ImproperlyConfigured(f"Unknown NOTIFICATIONS_PROVIDER {name!r}")
    return import_string(PROVIDERS[name])()
