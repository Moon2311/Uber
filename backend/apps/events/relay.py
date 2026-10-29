"""Outbox relay: publishes committed outbox rows to Kafka, in order.

* Single active relay: each batch runs in a transaction holding
  pg_try_advisory_xact_lock(RELAY_LOCK_ID). Extra relay processes find the lock taken
  and idle (hot standby), so there is no double publishing by design and no
  reordering between relays.
* Order: rows are published by ascending id. On the first failure the batch stops,
  the row gets exponential backoff and NOTHING after it is published until it
  succeeds (head-of-line blocking keeps per-key order; broker outages are global
  anyway).
* At-least-once: a crash after Kafka acked but before the row was marked
  PUBLISHED republishes it; consumers dedupe on event_id.
* After OUTBOX_MAX_ATTEMPTS a row is marked FAILED (alerting metric/log) so one
  poison event cannot block the stream forever.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from apps.common.metrics import metrics

from .kafka import OutgoingMessage
from .models import OutboxEvent, OutboxStatus
from .outbox import event_from_row
from .topics import physical

logger = logging.getLogger("apps.events")

RELAY_LOCK_ID = 0x0B07_0001  # arbitrary, stable


@dataclass(frozen=True)
class RelayResult:
    leader: bool
    published: int = 0
    failed: int = 0
    gave_up: int = 0
    backing_off: bool = False


def backoff_seconds(attempts: int) -> float:
    base = settings.OUTBOX_RETRY_BASE_SECONDS
    return min(settings.OUTBOX_RETRY_MAX_SECONDS, base * (2 ** max(0, attempts - 1)))


def _to_message(row: OutboxEvent) -> OutgoingMessage:
    event = event_from_row(row)
    headers = event.headers()
    if row.correlation_id:
        headers = [*headers, ("correlation_id", row.correlation_id.encode())]
    return OutgoingMessage(
        topic=physical(row.topic), key=row.key, value=event.to_bytes(), headers=headers
    )


class OutboxRelay:
    def __init__(self, publisher, *, clock=timezone.now):
        self.publisher = publisher
        self.clock = clock

    def run_once(self) -> RelayResult:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [RELAY_LOCK_ID])
                if not cursor.fetchone()[0]:
                    return RelayResult(leader=False)
            rows = list(
                OutboxEvent.objects.filter(status=OutboxStatus.PENDING).order_by("id")[
                    : settings.OUTBOX_BATCH_SIZE
                ]
            )
            now = self.clock()
            if not rows:
                return RelayResult(leader=True)
            if rows[0].next_attempt_at and rows[0].next_attempt_at > now:
                return RelayResult(leader=True, backing_off=True)

            results = self.publisher.publish(
                [_to_message(r) for r in rows], timeout=settings.KAFKA_DELIVERY_TIMEOUT_SECONDS
            )
            published = []
            for row, error in zip(rows, results, strict=True):
                if error is not None:
                    return self._failed(row, error, published, now)
                published.append(row.pk)
            self._mark_published(published, now)
            return RelayResult(leader=True, published=len(published))

    def _mark_published(self, ids, now):
        if ids:
            OutboxEvent.objects.filter(pk__in=ids).update(
                status=OutboxStatus.PUBLISHED, published_at=now, last_error=""
            )
            metrics().increment("events.outbox.published", len(ids))

    def _failed(self, row, error, published, now) -> RelayResult:
        # Everything before the failure was delivered; keep those.
        self._mark_published(published, now)
        row.attempts += 1
        row.last_error = error[:2000]
        gave_up = row.attempts >= settings.OUTBOX_MAX_ATTEMPTS
        if gave_up:
            row.status = OutboxStatus.FAILED
            row.next_attempt_at = None
            logger.error(
                "events.outbox.gave_up",
                extra={"event_id": str(row.event_id), "event_type": row.event_type,
                       "attempts": row.attempts, "error": row.last_error},
            )  # fmt: skip
            metrics().increment("events.outbox.gave_up")
        else:
            row.next_attempt_at = now + timedelta(seconds=backoff_seconds(row.attempts))
            logger.warning(
                "events.outbox.publish_failed",
                extra={"event_id": str(row.event_id), "attempts": row.attempts,
                       "error": row.last_error},
            )  # fmt: skip
            metrics().increment("events.outbox.publish_failed")
        row.save(update_fields=["attempts", "last_error", "status", "next_attempt_at"])
        return RelayResult(leader=True, published=len(published), failed=1, gave_up=int(gave_up))


def outbox_stats(now=None) -> dict:
    from django.db.models import Count, Min

    now = now or timezone.now()
    agg = OutboxEvent.objects.filter(status=OutboxStatus.PENDING).aggregate(
        pending=Count("id"), oldest=Min("created_at")
    )
    failed = OutboxEvent.objects.filter(status=OutboxStatus.FAILED).count()
    oldest_age = (now - agg["oldest"]).total_seconds() if agg["oldest"] else 0.0
    return {
        "pending": agg["pending"],
        "failed": failed,
        "oldest_pending_age_s": round(oldest_age, 1),
    }
