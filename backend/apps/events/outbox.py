"""Writing to the outbox. Must be called inside the business transaction."""

from datetime import datetime

from django.db import transaction

from apps.common.context import get_request_id
from apps.common.metrics import metrics

from .models import OutboxEvent
from .schemas import Event


def enqueue(event: Event) -> OutboxEvent:
    conn = transaction.get_connection()
    if not conn.in_atomic_block:
        # An outbox row outside a transaction would defeat the pattern: the event
        # could exist without the change (or vice versa).
        raise RuntimeError("outbox.enqueue() must run inside transaction.atomic()")
    row = OutboxEvent.objects.create(
        event_id=event.event_id,
        topic=event.topic,
        key=event.key,
        event_type=event.event_type,
        event_version=event.event_version,
        payload=event.payload,
        occurred_at=datetime.fromisoformat(event.occurred_at),
        correlation_id=get_request_id() or "",
    )
    metrics().increment("events.outbox.enqueued", tags={"event_type": event.event_type})
    return row


def event_from_row(row: OutboxEvent) -> Event:
    return Event(
        row.event_type,
        row.event_version,
        row.payload,
        event_id=str(row.event_id),
        occurred_at=row.occurred_at.isoformat(),
    )
