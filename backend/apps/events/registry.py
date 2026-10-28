"""Consumer groups run by `manage.py run_event_consumer <group>`.

Note what is *not* here: no group subscribes to driver.location.v1 by default.
High-frequency position data is opt-in per consumer (e.g. a future heat-map job).
"""

from django.utils.module_loading import import_string

from . import topics

CONSUMER_GROUPS = {
    "analytics": {
        "handlers": "apps.analytics.handlers.HANDLERS",
        "topics": [topics.TRIP_EVENTS, topics.DRIVER_AVAILABILITY],
    },
}


def group_config(name: str) -> tuple[list[str], dict]:
    cfg = CONSUMER_GROUPS[name]
    return cfg["topics"], import_string(cfg["handlers"])


def physical_group(name: str) -> str:
    from django.conf import settings

    return f"{settings.KAFKA_TOPIC_PREFIX}{name}"
