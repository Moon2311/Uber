"""Kafka topic catalogue: partitions, retention and cleanup policy per stream.

Topic names carry a *major* version (`.v1`): a breaking change gets a new topic
and both run side by side during migration. Additive changes bump the event's
`event_version` inside the envelope instead (see schemas.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS


@dataclass(frozen=True)
class TopicSpec:
    logical: str
    partitions: int
    retention_ms: int
    cleanup_policy: str = "delete"
    description: str = ""

    @property
    def name(self) -> str:
        return physical(self.logical)

    def config(self) -> dict[str, str]:
        cfg = {"retention.ms": str(self.retention_ms), "cleanup.policy": self.cleanup_policy}
        if "compact" in self.cleanup_policy:
            cfg["min.cleanable.dirty.ratio"] = "0.1"
            cfg["segment.ms"] = str(HOUR_MS)
        return cfg


DRIVER_LOCATION = "driver.location.v1"
DRIVER_AVAILABILITY = "driver.availability.v1"
TRIP_EVENTS = "trip.events.v1"
PAYMENT_EVENTS = "payment.events.v1"
DEAD_LETTER = "events.dlq.v1"

TOPICS: dict[str, TopicSpec] = {
    spec.logical: spec
    for spec in [
        TopicSpec(
            DRIVER_LOCATION,
            partitions=6,
            retention_ms=1 * HOUR_MS,
            description="Sampled driver positions (cell change or >=15 s). Best effort, "
            "not every GPS ping. Short retention: stale positions are worthless.",
        ),
        TopicSpec(
            DRIVER_AVAILABILITY,
            partitions=3,
            retention_ms=7 * DAY_MS,
            cleanup_policy="compact,delete",
            description="Online/offline transitions. Compacted: latest state per driver "
            "is kept for bootstrapping consumers.",
        ),
        TopicSpec(
            TRIP_EVENTS,
            partitions=6,
            retention_ms=7 * DAY_MS,
            description="Every trip lifecycle event (RideRequested ... TripCompleted) keyed "
            "by trip_id, so one trip's events stay ordered in one partition.",
        ),
        TopicSpec(
            PAYMENT_EVENTS,
            partitions=6,
            retention_ms=7 * DAY_MS,
            description="Payment status changes keyed by trip_id. Informational: the "
            "payment record in PostgreSQL (reconciled with the provider) is authoritative.",
        ),
        TopicSpec(
            DEAD_LETTER,
            partitions=3,
            retention_ms=14 * DAY_MS,
            description="Messages a consumer could not process after retries; original "
            "bytes + diagnostic headers.",
        ),
    ]
}


def physical(logical: str) -> str:
    """Physical topic name (a prefix isolates environments / test runs)."""
    return f"{settings.KAFKA_TOPIC_PREFIX}{logical}"


def logical(physical_name: str) -> str:
    prefix = settings.KAFKA_TOPIC_PREFIX
    return (
        physical_name[len(prefix) :]
        if prefix and physical_name.startswith(prefix)
        else (physical_name)
    )
