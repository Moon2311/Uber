from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.events.models import ConsumerHeartbeat, OutboxEvent, OutboxStatus, ProcessedEvent


class Command(BaseCommand):
    help = (
        "Delete published outbox rows and old consumer-dedupe records. The dedupe "
        "window (PROCESSED_EVENT_RETENTION_DAYS) must exceed the longest topic "
        "retention so a redelivered event is always recognised. Run daily."
    )

    def handle(self, *args, **options):
        now = timezone.now()
        outbox = OutboxEvent.objects.filter(
            status=OutboxStatus.PUBLISHED,
            published_at__lt=now - timedelta(days=settings.OUTBOX_RETENTION_DAYS),
        ).delete()[0]
        processed = ProcessedEvent.objects.filter(
            processed_at__lt=now - timedelta(days=settings.PROCESSED_EVENT_RETENTION_DAYS)
        ).delete()[0]
        # Each consumer process registers its own heartbeat row; drop dead instances.
        dead = ConsumerHeartbeat.objects.filter(last_seen__lt=now - timedelta(days=1))
        heartbeats = dead.delete()[0]
        self.stdout.write(
            f"Deleted {outbox} outbox row(s), {processed} dedupe record(s), "
            f"{heartbeats} stale heartbeat(s)."
        )
