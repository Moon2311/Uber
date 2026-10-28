import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Print outbox backlog, Kafka reachability, consumer lag and heartbeats."

    def handle(self, *args, **options):
        from apps.events.monitoring import detailed

        self.stdout.write(json.dumps(detailed(), indent=2, default=str))
