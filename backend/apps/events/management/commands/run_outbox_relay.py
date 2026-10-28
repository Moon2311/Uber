import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import DatabaseError, connection

from apps.events.relay import OutboxRelay

logger = logging.getLogger("apps.events")


class Command(BaseCommand):
    help = "Publish committed outbox events to Kafka (run as a long-lived process)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Drain once and exit.")

    def handle(self, *args, once, **options):
        from apps.events.kafka import get_publisher

        relay = OutboxRelay(get_publisher())
        stop = {"flag": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
        signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
        logger.info("events.relay.started")
        total = 0
        while not stop["flag"]:
            try:
                result = relay.run_once()
            except DatabaseError:
                logger.exception("events.relay.database_error")
                connection.close()
                time.sleep(settings.OUTBOX_POLL_INTERVAL_SECONDS * 10)
                continue
            total += result.published
            if once and (result.published == 0 or result.failed):
                break
            # Busy batch -> loop immediately; otherwise poll.
            if result.published < settings.OUTBOX_BATCH_SIZE or result.failed:
                time.sleep(settings.OUTBOX_POLL_INTERVAL_SECONDS)
        self.stdout.write(f"Relay stopped; published {total} event(s).")
