"""Blocking pre-action checks, configured per action in settings.ACTION_GUARDS.

    ACTION_GUARDS = {"trip_request": ["apps.payments.guards.no_outstanding_balance",
                                      "apps.fraud.guards.check"], ...}

A guard is called as guard(action=..., user=...) and raises an APIException to
deny. This keeps domains decoupled: the trip service does not import payments or
fraud, it just asks "may this user do this?". Guards run synchronously, on the
request path, inside the caller's transaction: only checks that MUST block go
here; everything else is asynchronous monitoring (Kafka consumers).
"""

from functools import lru_cache

from django.conf import settings
from django.utils.module_loading import import_string


@lru_cache(maxsize=32)
def _load(paths: tuple[str, ...]):
    return [import_string(p) for p in paths]


def run_guards(action: str, *, user) -> None:
    for guard in _load(tuple(settings.ACTION_GUARDS.get(action, ()))):
        guard(action=action, user=user)
