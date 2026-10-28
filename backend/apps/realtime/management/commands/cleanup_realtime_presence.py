from django.core.management.base import BaseCommand

from apps.realtime.presence import cleanup_stale


class Command(BaseCommand):
    help = "Remove presence entries of WebSocket connections that stopped heartbeating."

    def handle(self, *args, **options):
        self.stdout.write(f"Removed {cleanup_stale()} stale connection(s).")
