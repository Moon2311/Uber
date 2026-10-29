"""Consumer framework: idempotent handling, retries, dead-lettering.

Failure classes and what happens:
  * SchemaError / unknown event version / PermanentError
        -> dead-letter immediately (retrying cannot help)
  * any other handler exception (bug, transient dependency error)
        -> retried CONSUMER_MAX_RETRIES times with exponential backoff, then DLQ
  * infrastructure errors (PostgreSQL down) or DLQ produce failure
        -> NOT dead-lettered: raised to the run loop, which does not commit and
           retries the same message after a pause (otherwise an outage would
           dump the whole stream into the DLQ)
Offsets are committed only after a message is handled, duplicated or
dead-lettered: at-least-once delivery + idempotent handlers = effectively once.
"""

from __future__ import annotations

import enum
import logging
import signal
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from django.conf import settings
from django.db import DatabaseError, IntegrityError, InterfaceError, OperationalError, transaction

from apps.common.metrics import metrics

from .models import ConsumerHeartbeat, ProcessedEvent
from .schemas import Event, SchemaError, decode
from .topics import DEAD_LETTER, logical, physical

logger = logging.getLogger("apps.events")


class PermanentError(Exception):
    """Raise from a handler when retrying can never succeed."""


class InfrastructureError(Exception):
    """Processing cannot continue right now; do not commit, retry later."""


INFRA_ERRORS = (OperationalError, InterfaceError)


class Outcome(str, enum.Enum):
    HANDLED = "handled"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"  # event type this group does not handle
    DEAD_LETTERED = "dead_lettered"


@dataclass(frozen=True)
class ConsumedMessage:
    topic: str
    partition: int
    offset: int
    key: str | None
    value: bytes
    headers: list[tuple[str, bytes]] = field(default_factory=list)

    @classmethod
    def from_kafka(cls, msg) -> ConsumedMessage:
        key = msg.key()
        return cls(
            topic=msg.topic(),
            partition=msg.partition(),
            offset=msg.offset(),
            key=key.decode() if isinstance(key, bytes) else key,
            value=msg.value() or b"",
            headers=list(msg.headers() or []),
        )


Handler = Callable[[Event], None]


class EventProcessor:
    def __init__(
        self,
        group: str,
        handlers: dict[str, Handler],
        dlq_publisher,
        *,
        max_retries: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.group = group
        self.handlers = handlers
        self.dlq = dlq_publisher
        self.max_retries = settings.CONSUMER_MAX_RETRIES if max_retries is None else max_retries
        self.sleep = sleep

    def process(self, msg: ConsumedMessage) -> Outcome:
        from apps.common.context import bound_request_id

        correlation = next((v for k, v in msg.headers if k == "correlation_id"), None)
        if isinstance(correlation, bytes):
            correlation = correlation.decode("ascii", "replace")
        # Handler logs carry the id of the API request that caused the event.
        with bound_request_id(correlation):
            return self._process(msg)

    def _process(self, msg: ConsumedMessage) -> Outcome:
        try:
            event = decode(msg.value)
        except SchemaError as exc:
            return self._dead_letter(msg, exc, attempts=0, event=None)
        handler = self.handlers.get(event.event_type)
        if handler is None:
            metrics().increment("events.consumer.ignored", tags={"group": self.group})
            return Outcome.IGNORED

        attempt = 0
        while True:
            attempt += 1
            try:
                return self._handle_once(event, handler)
            except INFRA_ERRORS as exc:
                raise InfrastructureError(str(exc)) from exc
            except PermanentError as exc:
                return self._dead_letter(msg, exc, attempts=attempt, event=event)
            except Exception as exc:  # noqa: BLE001 - classified as retryable
                if attempt > self.max_retries:
                    return self._dead_letter(msg, exc, attempts=attempt, event=event)
                delay = min(
                    settings.CONSUMER_RETRY_MAX_SECONDS,
                    settings.CONSUMER_RETRY_BASE_SECONDS * 2 ** (attempt - 1),
                )
                metrics().increment("events.consumer.retry", tags={"group": self.group})
                logger.warning(
                    "events.consumer.retry",
                    extra={"group": self.group, "event_id": event.event_id,
                           "attempt": attempt, "error": repr(exc)[:300]},
                )  # fmt: skip
                self.sleep(delay)

    def _handle_once(self, event: Event, handler: Handler) -> Outcome:
        if ProcessedEvent.objects.filter(
            consumer_group=self.group, event_id=event.event_id
        ).exists():
            metrics().increment("events.consumer.duplicate", tags={"group": self.group})
            return Outcome.DUPLICATE
        try:
            with transaction.atomic():
                # Ledger row + side effects commit (or roll back) together.
                ProcessedEvent.objects.create(
                    consumer_group=self.group, event_id=event.event_id, event_type=event.event_type
                )
                handler(event)
        except IntegrityError:
            if ProcessedEvent.objects.filter(
                consumer_group=self.group, event_id=event.event_id
            ).exists():
                # A concurrent delivery (e.g. during a rebalance) won the race.
                metrics().increment("events.consumer.duplicate", tags={"group": self.group})
                return Outcome.DUPLICATE
            raise
        metrics().increment(
            "events.consumer.handled", tags={"group": self.group, "event_type": event.event_type}
        )
        return Outcome.HANDLED

    def _dead_letter(self, msg, exc, *, attempts, event) -> Outcome:
        from .kafka import OutgoingMessage

        headers = list(msg.headers) + [
            ("dlq_original_topic", msg.topic.encode()),
            ("dlq_original_partition", str(msg.partition).encode()),
            ("dlq_original_offset", str(msg.offset).encode()),
            ("dlq_consumer_group", self.group.encode()),
            ("dlq_error_class", type(exc).__name__.encode()),
            ("dlq_error", str(exc)[:1000].encode()),
            ("dlq_attempts", str(attempts).encode()),
            ("dlq_failed_at", datetime.now(timezone.utc).isoformat().encode()),
        ]
        [error] = self.dlq.publish(
            [OutgoingMessage(physical(DEAD_LETTER), msg.key or "", msg.value, headers)],
            timeout=settings.KAFKA_DELIVERY_TIMEOUT_SECONDS,
        )
        if error is not None:
            # Never commit past a message we could neither process nor park.
            raise InfrastructureError(f"DLQ publish failed: {error}")
        metrics().increment(
            "events.consumer.dead_lettered",
            tags={"group": self.group, "error": type(exc).__name__},
        )
        logger.error(
            "events.consumer.dead_lettered",
            extra={"group": self.group, "topic": logical(msg.topic), "offset": msg.offset,
                   "event_id": getattr(event, "event_id", None), "error": repr(exc)[:300]},
        )  # fmt: skip
        return Outcome.DEAD_LETTERED


class ConsumerRunner:
    """Poll loop around EventProcessor with manual commits and graceful shutdown."""

    def __init__(self, group: str, logical_topics: list[str], handlers: dict[str, Handler]):
        from .kafka import get_publisher, make_consumer

        self.group = group
        self.topics = [physical(t) for t in logical_topics]
        self.consumer = make_consumer(group)
        self.processor = EventProcessor(group, handlers, get_publisher())
        self.instance = f"{socket.gethostname()}:{id(self)}"
        self._stop = False
        self._counts = {"processed": 0, "dead_lettered": 0}
        self._last_beat = 0.0

    def stop(self, *_):
        self._stop = True

    def run(self, *, max_messages: int | None = None, idle_timeout: float | None = None):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.consumer.subscribe(self.topics)
        logger.info("events.consumer.started", extra={"group": self.group, "topics": self.topics})
        handled, idle_since = 0, time.monotonic()
        try:
            while not self._stop:
                self._heartbeat()
                raw = self.consumer.poll(1.0)
                if raw is None:
                    if idle_timeout is not None and time.monotonic() - idle_since > idle_timeout:
                        break
                    continue
                if raw.error():
                    logger.warning("events.consumer.kafka_error", extra={"error": str(raw.error())})
                    continue
                idle_since = time.monotonic()
                msg = ConsumedMessage.from_kafka(raw)
                try:
                    outcome = self.processor.process(msg)
                except InfrastructureError as exc:
                    logger.error("events.consumer.infrastructure", extra={"error": str(exc)})
                    metrics().increment("events.consumer.infrastructure_error")
                    # Rewind to this message and pause; nothing is committed.
                    from confluent_kafka import TopicPartition

                    self.consumer.seek(TopicPartition(msg.topic, msg.partition, msg.offset))
                    time.sleep(settings.CONSUMER_INFRA_PAUSE_SECONDS)
                    _reset_db_connection()
                    continue
                self.consumer.commit(message=raw, asynchronous=False)
                self._counts["processed"] += 1
                self._counts["dead_lettered"] += outcome is Outcome.DEAD_LETTERED
                handled += 1
                if max_messages is not None and handled >= max_messages:
                    break
        finally:
            self.consumer.close()
            logger.info("events.consumer.stopped", extra={"group": self.group})

    def _heartbeat(self):
        from apps.common.worker import beat

        beat()  # container liveness (every poll); the DB heartbeat below is throttled
        now = time.monotonic()
        if now - self._last_beat < settings.CONSUMER_HEARTBEAT_SECONDS:
            return
        self._last_beat = now
        try:
            ConsumerHeartbeat.objects.update_or_create(
                consumer_group=self.group,
                instance=self.instance,
                defaults={"last_seen": datetime.now(timezone.utc), **self._counts},
            )
        except DatabaseError:
            _reset_db_connection()


def _reset_db_connection():
    from django.db import connection

    try:
        connection.close()
    except DatabaseError:
        pass
