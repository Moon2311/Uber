from django.conf import settings
from django.core.management.base import BaseCommand

from apps.common.worker import run_loop
from apps.notifications.services import dispatch_due


class Command(BaseCommand):
    help = "Deliver due push notifications with retries. Safe to run several replicas."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, once, **options):
        run_loop("notifications.worker",
                 lambda: dispatch_due(limit=settings.NOTIFICATIONS_WORKER_BATCH),
                 interval=settings.NOTIFICATIONS_WORKER_POLL_SECONDS, once=once)  # fmt: skip
