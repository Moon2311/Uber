import time

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create Kafka topics (partitions, retention, cleanup policy) if missing. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--wait", type=int, default=0, help="Seconds to wait for Kafka.")

    def handle(self, *args, wait, **options):
        from confluent_kafka import KafkaException

        from apps.events.kafka import ensure_topics

        deadline = time.monotonic() + wait
        while True:
            try:
                result = ensure_topics()
                break
            except KafkaException as exc:
                if time.monotonic() >= deadline:
                    raise CommandError(f"Kafka unavailable: {exc}") from exc
                time.sleep(2)
        for name, state in sorted(result.items()):
            self.stdout.write(f"{name}: {state}")
