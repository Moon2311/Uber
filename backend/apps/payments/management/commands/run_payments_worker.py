from django.conf import settings
from django.core.management.base import BaseCommand

from apps.common.worker import run_loop
from apps.payments.services import charge_due_payments, reconcile_open_attempts


class Command(BaseCommand):
    help = (
        "Charge card payments of completed trips and reconcile ambiguous / pending "
        "provider outcomes. Safe to run several replicas (SKIP LOCKED + leases)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, once, **options):
        def step():
            return charge_due_payments(limit=settings.PAYMENTS_WORKER_BATCH) + (
                reconcile_open_attempts(limit=settings.PAYMENTS_WORKER_BATCH)
            )

        run_loop("payments.worker", step, interval=settings.PAYMENTS_WORKER_POLL_SECONDS,
                 once=once)  # fmt: skip
