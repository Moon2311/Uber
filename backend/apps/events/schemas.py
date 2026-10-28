"""Versioned event schemas and the envelope every event travels in.

Envelope (JSON, UTF-8):
    {
      "event_id": "<uuid4>",          # unique; consumers dedupe on it
      "event_type": "TripStatusChanged",
      "event_version": 1,             # schema version of `payload`
      "occurred_at": "2026-10-28T14:05:25.154518+00:00",
      "producer": "ride-hailing-backend",
      "key": "<partition key>",       # driver_id for driver events, trip_id for trips
      "payload": {...}
    }

Compatibility rules for a given (event_type, major topic):
  * adding an OPTIONAL field              -> bump event_version, same topic
  * removing/renaming/retyping a field    -> new event_version AND consumers must
                                             handle both; if not feasible, new topic
  * consumers must ignore unknown fields and reject unknown versions (DLQ)
Money is always a decimal *string*; timestamps are ISO-8601 with offset (UTC).
Payloads carry ids, never names/emails/phone numbers.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from . import topics

PRODUCER = "ride-hailing-backend"


class SchemaError(ValueError):
    """Payload or envelope does not match a known schema (permanent failure)."""


@dataclass(frozen=True)
class F:
    name: str
    type: str  # str | int | float | bool | decimal | datetime | uuid
    required: bool = True
    nullable: bool = False


@dataclass(frozen=True)
class EventSchema:
    event_type: str
    version: int
    topic: str  # logical topic
    key_field: str
    fields: tuple[F, ...]
    description: str = ""

    def validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise SchemaError(f"{self.event_type}.v{self.version}: payload must be an object")
        out = {}
        for f in self.fields:
            if f.name not in payload or payload[f.name] is None:
                if f.name in payload and f.nullable:
                    out[f.name] = None
                    continue
                if f.required:
                    raise SchemaError(f"{self.event_type}.v{self.version}: missing {f.name}")
                out[f.name] = None
                continue
            out[f.name] = _coerce(self, f, payload[f.name])
        return out  # unknown fields are dropped (forward compatibility)

    def json_schema(self) -> dict:
        types = {
            "str": {"type": "string"},
            "int": {"type": "integer"},
            "float": {"type": "number"},
            "bool": {"type": "boolean"},
            "decimal": {"type": "string", "pattern": r"^-?\d+(\.\d+)?$"},
            "datetime": {"type": "string", "format": "date-time"},
            "uuid": {"type": "string", "format": "uuid"},
        }
        props = {}
        for f in self.fields:
            spec = dict(types[f.type])
            if f.nullable:
                spec = {"anyOf": [spec, {"type": "null"}]}
            props[f.name] = spec
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": f"{self.event_type} v{self.version}",
            "description": self.description,
            "type": "object",
            "properties": props,
            "required": [f.name for f in self.fields if f.required],
        }


def _coerce(schema: EventSchema, f: F, value):
    where = f"{schema.event_type}.v{schema.version}.{f.name}"
    try:
        if f.type == "str":
            if not isinstance(value, str):
                raise TypeError
            return value
        if f.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError
            return value
        if f.type == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError
            return float(value)
        if f.type == "bool":
            if not isinstance(value, bool):
                raise TypeError
            return value
        if f.type == "decimal":
            if isinstance(value, float):
                raise TypeError  # floats lose money precision
            return str(Decimal(str(value)))
        if f.type == "datetime":
            dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
            if dt.tzinfo is None:
                raise ValueError("naive datetime")
            return dt.astimezone(timezone.utc).isoformat()
        if f.type == "uuid":
            return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise SchemaError(f"{where}: invalid {f.type} {value!r}") from exc
    raise SchemaError(f"{where}: unknown field type {f.type}")


def _schema(event_type, version, topic, key_field, *fields, description=""):
    return EventSchema(event_type, version, topic, key_field, tuple(fields), description)


SCHEMAS: dict[tuple[str, int], EventSchema] = {
    (s.event_type, s.version): s
    for s in [
        _schema(
            "DriverLocationUpdated",
            1,
            topics.DRIVER_LOCATION,
            "driver_id",
            F("driver_id", "int"),
            F("lat", "decimal"),
            F("lng", "decimal"),
            F("recorded_at", "datetime"),
            F("h3_cell", "str", nullable=True),
            F("h3_resolution", "int", nullable=True),
            F("heading", "float", required=False, nullable=True),
            F("speed_kmh", "float", required=False, nullable=True),
            F("reason", "str"),  # cell_changed | interval
            description="Sampled driver position. NOT every GPS ping.",
        ),
        _schema(
            "DriverAvailabilityChanged",
            1,
            topics.DRIVER_AVAILABILITY,
            "driver_id",
            F("driver_id", "int"),
            F("status", "str"),
            F("previous_status", "str", nullable=True),
            F("reason", "str"),  # driver_request | stale_location
            F("vehicle_category", "str", nullable=True),
            F("changed_at", "datetime"),
        ),
        _schema(
            "RideRequested",
            1,
            topics.TRIP_EVENTS,
            "trip_id",
            F("trip_id", "uuid"),
            F("rider_id", "int"),
            F("ride_category", "str"),
            F("pickup_lat", "decimal"),
            F("pickup_lng", "decimal"),
            F("dropoff_lat", "decimal"),
            F("dropoff_lng", "decimal"),
            F("pickup_h3_cell", "str", nullable=True),
            F("estimated_fare", "decimal"),
            F("currency", "str"),
            F("requested_at", "datetime"),
        ),
        _schema(
            "RideOfferCreated",
            1,
            topics.TRIP_EVENTS,
            "trip_id",
            F("trip_id", "uuid"),
            F("offer_id", "uuid"),
            F("driver_id", "int"),
            F("expires_at", "datetime"),
            F("straight_line_km", "float", nullable=True),
            F("grid_distance", "int", nullable=True),
            F("created_at", "datetime"),
        ),
        _schema(
            "RideAccepted",
            1,
            topics.TRIP_EVENTS,
            "trip_id",
            F("trip_id", "uuid"),
            F("offer_id", "uuid"),
            F("driver_id", "int"),
            F("accepted_at", "datetime"),
        ),
        _schema(
            "TripStatusChanged",
            1,
            topics.TRIP_EVENTS,
            "trip_id",
            F("trip_id", "uuid"),
            F("from_status", "str", nullable=True),
            F("to_status", "str"),
            F("rider_id", "int"),
            F("driver_id", "int", nullable=True),
            F("reason", "str", required=False, nullable=True),
            F("changed_at", "datetime"),
        ),
        _schema(
            "TripCompleted",
            1,
            topics.TRIP_EVENTS,
            "trip_id",
            F("trip_id", "uuid"),
            F("rider_id", "int"),
            F("driver_id", "int"),
            F("ride_category", "str"),
            F("estimated_fare", "decimal"),
            F("final_fare", "decimal", nullable=True),
            F("currency", "str"),
            F("estimated_distance_km", "decimal"),
            F("started_at", "datetime", nullable=True),
            F("completed_at", "datetime"),
            description="Informational. The trip and payment records in PostgreSQL "
            "remain authoritative; never settle payments from this event.",
        ),
    ]
}


def schema_for(event_type: str, version: int) -> EventSchema:
    try:
        return SCHEMAS[(event_type, version)]
    except KeyError:
        raise SchemaError(f"Unknown event {event_type} v{version}") from None


@dataclass(frozen=True)
class Event:
    event_type: str
    event_version: int
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    producer: str = PRODUCER

    @property
    def schema(self) -> EventSchema:
        return schema_for(self.event_type, self.event_version)

    @property
    def key(self) -> str:
        return str(self.payload[self.schema.key_field])

    @property
    def topic(self) -> str:
        return self.schema.topic

    def envelope(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_version": self.event_version,
            "occurred_at": self.occurred_at,
            "producer": self.producer,
            "key": self.key,
            "payload": self.payload,
        }

    def to_bytes(self) -> bytes:
        return json.dumps(self.envelope(), separators=(",", ":")).encode()

    def headers(self) -> list[tuple[str, bytes]]:
        return [
            ("event_type", self.event_type.encode()),
            ("event_version", str(self.event_version).encode()),
            ("event_id", self.event_id.encode()),
            ("content_type", b"application/json"),
        ]


def make_event(event_type: str, payload: dict[str, Any], *, version: int = 1, **kw) -> Event:
    """Build and validate an event (raises SchemaError)."""
    schema = schema_for(event_type, version)
    return Event(event_type, version, schema.validate(payload), **kw)


def decode(raw: bytes) -> Event:
    """Parse + validate an envelope coming off Kafka (raises SchemaError)."""
    try:
        env = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise SchemaError(f"Not JSON: {exc}") from exc
    if not isinstance(env, dict):
        raise SchemaError("Envelope must be an object")
    for name in ("event_id", "event_type", "event_version", "occurred_at", "payload"):
        if name not in env:
            raise SchemaError(f"Envelope missing {name}")
    try:
        event_id = str(uuid.UUID(str(env["event_id"])))
    except ValueError as exc:
        raise SchemaError("Invalid event_id") from exc
    if not isinstance(env["event_version"], int):
        raise SchemaError("event_version must be an integer")
    schema = schema_for(env["event_type"], env["event_version"])
    return Event(
        env["event_type"],
        env["event_version"],
        schema.validate(env["payload"]),
        event_id=event_id,
        occurred_at=env["occurred_at"],
        producer=env.get("producer", "unknown"),
    )
