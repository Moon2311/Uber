"""Kafka adapter: the only module importing confluent_kafka.

Producer settings favour correctness for business events:
  enable.idempotence=true  -> no duplicates/reordering from producer retries
  acks=all                 -> written to all in-sync replicas before success
  max.in.flight<=5         -> required by idempotence, keeps per-partition order
Delivery is still at-least-once end to end (relay may crash after the broker ack
but before marking the outbox row), so consumers dedupe on event_id.
"""

from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass
from functools import lru_cache

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic
from django.conf import settings

from apps.common.metrics import metrics

from .topics import TOPICS, physical

logger = logging.getLogger("apps.events")


class PublishError(Exception):
    pass


@dataclass(frozen=True)
class OutgoingMessage:
    topic: str  # physical
    key: str
    value: bytes
    headers: list[tuple[str, bytes]]


def _base_config() -> dict:
    return {
        "bootstrap.servers": settings.KAFKA_BOOTSTRAP_SERVERS,
        "client.id": f"{settings.KAFKA_CLIENT_ID}-{socket.gethostname()}",
        # Reconnect quickly after a broker restart (defaults: 10 s backoff cap and a
        # 30 s connection-setup timeout, which delayed consumer recovery by ~30 s).
        "reconnect.backoff.max.ms": 2000,
        "socket.connection.setup.timeout.ms": 5000,
        # One "broker down" log line per minute, not one per reconnect attempt.
        "log.connection.close": False,
    }


class KafkaPublisher:
    """Synchronous batch publishing (outbox relay, DLQ) + non-blocking produce
    (sampled location stream)."""

    def __init__(self, config: dict | None = None):
        self._producer = Producer(
            {
                **_base_config(),
                "enable.idempotence": True,
                "acks": "all",
                "max.in.flight.requests.per.connection": 5,
                "compression.type": "lz4",
                "linger.ms": 20,
                "message.timeout.ms": int(settings.KAFKA_DELIVERY_TIMEOUT_SECONDS * 1000),
                # Bounded local queue: if Kafka is down, best-effort messages are
                # dropped instead of growing memory without limit.
                "queue.buffering.max.messages": 20000,
                **(config or {}),
            }
        )

    def publish(self, messages: list[OutgoingMessage], *, timeout: float) -> list[str | None]:
        """Send in order and wait. Returns one entry per message: None on success,
        otherwise an error string. Order within a partition is preserved."""
        results: list[str | None] = ["not delivered (timeout)"] * len(messages)

        def callback_for(i):
            def cb(err, _msg):
                results[i] = None if err is None else str(err)

            return cb

        for i, m in enumerate(messages):
            try:
                self._producer.produce(
                    m.topic,
                    key=m.key,
                    value=m.value,
                    headers=m.headers,
                    on_delivery=callback_for(i),
                )
            except (BufferError, KafkaException) as exc:
                results[i] = f"produce failed: {exc}"
                break
            self._producer.poll(0)
        remaining = self._producer.flush(timeout)
        if remaining:
            logger.warning("events.kafka.flush_incomplete", extra={"remaining": remaining})
        return results

    def produce_nowait(self, message: OutgoingMessage) -> bool:
        """Fire-and-forget. False if the local queue is full (message dropped)."""
        try:
            self._producer.produce(
                message.topic,
                key=message.key,
                value=message.value,
                headers=message.headers,
                on_delivery=_nowait_callback,
            )
        except BufferError:
            metrics().increment("events.produce.dropped", tags={"topic": message.topic})
            return False
        self._producer.poll(0)
        return True

    def flush(self, timeout: float = 5.0) -> int:
        return self._producer.flush(timeout)


def _nowait_callback(err, msg):
    if err is not None:
        metrics().increment("events.produce.failed", tags={"topic": msg.topic()})
    else:
        metrics().increment("events.produce.delivered", tags={"topic": msg.topic()})


_lock = threading.Lock()


@lru_cache(maxsize=4)
def _shared_publisher(bootstrap: str) -> KafkaPublisher:
    return KafkaPublisher()


def get_publisher() -> KafkaPublisher:
    with _lock:
        return _shared_publisher(settings.KAFKA_BOOTSTRAP_SERVERS)


def make_consumer(group_id: str, **overrides) -> Consumer:
    return Consumer(
        {
            **_base_config(),
            "group.id": group_id,
            "enable.auto.commit": False,  # commit only after processing (at-least-once)
            "auto.offset.reset": "earliest",
            "enable.partition.eof": False,
            "session.timeout.ms": 15000,
            "max.poll.interval.ms": 300000,
            **overrides,
        }
    )


def admin(bootstrap: str | None = None) -> AdminClient:
    cfg = _base_config()
    if bootstrap:
        cfg["bootstrap.servers"] = bootstrap
    return AdminClient(cfg)


def ensure_topics(timeout: float = 15.0) -> dict[str, str]:
    """Create missing topics with their configured partitions/retention. Idempotent."""
    client = admin()
    existing = set(client.list_topics(timeout=timeout).topics)
    wanted = [
        NewTopic(
            spec.name,
            num_partitions=spec.partitions,
            replication_factor=settings.KAFKA_REPLICATION_FACTOR,
            config=spec.config(),
        )
        for spec in TOPICS.values()
        if spec.name not in existing
    ]
    result = {spec.name: "exists" for spec in TOPICS.values() if spec.name in existing}
    if wanted:
        for name, future in client.create_topics(wanted, request_timeout=timeout).items():
            try:
                future.result()
                result[name] = "created"
            except KafkaException as exc:
                if exc.args[0].code() == KafkaError.TOPIC_ALREADY_EXISTS:
                    result[name] = "exists"
                else:
                    raise
    return result


def delete_topics(names: list[str], timeout: float = 15.0, bootstrap: str | None = None) -> None:
    if not names:
        return
    # Keep the client referenced until the futures resolve: if it is garbage
    # collected first, librdkafka destroys the handle and every future fails.
    client = admin(bootstrap)
    for future in client.delete_topics(names, operation_timeout=timeout).values():
        try:
            future.result()
        except KafkaException as exc:
            if exc.args[0].code() != KafkaError.UNKNOWN_TOPIC_OR_PART:
                raise


def ping(timeout: float = 1.0) -> dict:
    try:
        md = admin().list_topics(timeout=timeout)
    except KafkaException as exc:
        return {"status": "unavailable", "error": str(exc)[:200]}
    return {"status": "ok", "brokers": len(md.brokers)}


def consumer_lag(group_id: str, logical_topics: list[str], timeout: float = 5.0) -> dict:
    """Per-partition committed offset, high watermark and lag for a consumer group."""
    consumer = make_consumer(group_id)
    try:
        md = consumer.list_topics(timeout=timeout)
        partitions = []
        for lt in logical_topics:
            t = md.topics.get(physical(lt))
            if t is None or t.error is not None:
                continue
            partitions += [TopicPartition(t.topic, p) for p in t.partitions]
        committed = consumer.committed(partitions, timeout=timeout) if partitions else []
        out, total = [], 0
        for tp in committed:
            low, high = consumer.get_watermark_offsets(tp, timeout=timeout, cached=False)
            # No commit yet -> everything still retained is lag.
            position = tp.offset if tp.offset >= 0 else low
            lag = max(0, high - position)
            total += lag
            out.append(
                {"topic": tp.topic, "partition": tp.partition, "committed": tp.offset,
                 "high_watermark": high, "lag": lag}
            )  # fmt: skip
        return {"group": group_id, "total_lag": total, "partitions": out}
    finally:
        consumer.close()
