import uuid

from django.db import models


class OutboxStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PUBLISHED = "PUBLISHED", "Published"
    FAILED = "FAILED", "Failed (gave up)"


class OutboxEvent(models.Model):
    """Transactional outbox: written in the SAME PostgreSQL transaction as the
    business change, published to Kafka later by the relay. If the transaction
    rolls back, the event never existed; if Kafka is down, events wait here."""

    id = models.BigAutoField(primary_key=True)  # insertion order = publish order
    event_id = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    topic = models.CharField(max_length=100)  # logical topic
    key = models.CharField(max_length=100)
    event_type = models.CharField(max_length=60)
    event_version = models.PositiveSmallIntegerField()
    payload = models.JSONField()
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=10, choices=OutboxStatus.choices, default=OutboxStatus.PENDING
    )
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["id"],
                name="outbox_pending_idx",
                condition=models.Q(status="PENDING"),
            ),
            models.Index(fields=["status", "published_at"], name="outbox_status_pub_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=OutboxStatus.values), name="outbox_status_valid"
            ),
        ]

    def __str__(self):
        return f"{self.event_type} {self.event_id} [{self.status}]"


class ProcessedEvent(models.Model):
    """Idempotency ledger for consumers: (consumer group, event_id) is inserted in
    the same transaction as the handler's side effects."""

    consumer_group = models.CharField(max_length=100)
    event_id = models.UUIDField()
    event_type = models.CharField(max_length=60)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["consumer_group", "event_id"], name="processed_event_once"
            )
        ]
        indexes = [models.Index(fields=["processed_at"], name="processed_at_idx")]

    def __str__(self):
        return f"{self.consumer_group}:{self.event_id}"


class ConsumerHeartbeat(models.Model):
    consumer_group = models.CharField(max_length=100)
    instance = models.CharField(max_length=200)
    last_seen = models.DateTimeField()
    processed = models.PositiveBigIntegerField(default=0)
    dead_lettered = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["consumer_group", "instance"], name="consumer_heartbeat_unique"
            )
        ]

    def __str__(self):
        return f"{self.consumer_group}@{self.instance}"
