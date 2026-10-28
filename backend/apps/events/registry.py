"""Consumer groups run by `manage.py run_event_consumer <group>`.

High-frequency position data is opt-in per consumer: only `fraud` subscribes to
the sampled driver.location.v1 stream (speed plausibility).
"""

from django.utils.module_loading import import_string

from . import topics

CONSUMER_GROUPS = {
    "analytics": {
        "handlers": "apps.analytics.handlers.HANDLERS",
        "topics": [topics.TRIP_EVENTS, topics.DRIVER_AVAILABILITY],
    },
    # Asynchronous fraud monitoring; the only default subscriber to sampled GPS.
    "fraud": {
        "handlers": "apps.fraud.handlers.HANDLERS",
        "topics": [topics.TRIP_EVENTS, topics.PAYMENT_EVENTS, topics.DRIVER_LOCATION],
    },
    "notifications": {
        "handlers": "apps.notifications.services.HANDLERS",
        "topics": [topics.TRIP_EVENTS, topics.PAYMENT_EVENTS],
    },
}


def group_config(name: str) -> tuple[list[str], dict]:
    cfg = CONSUMER_GROUPS[name]
    return cfg["topics"], import_string(cfg["handlers"])


def physical_group(name: str) -> str:
    from django.conf import settings

    return f"{settings.KAFKA_TOPIC_PREFIX}{name}"
