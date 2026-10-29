import json

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.common.ops_metrics import collect
from apps.common.worker import run_loop


class Command(BaseCommand):
    help = (
        "Publish system gauges (location freshness, backlogs, consumer lag, payments, "
        "database health) for Prometheus. Run exactly one (more is harmless)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Collect once and print JSON")

    def handle(self, *args, once, **options):
        if once:
            self.stdout.write(json.dumps(collect(), indent=2, sort_keys=True, default=str))
            return
        run_loop("ops.monitor", lambda: collect() and 0,
                 interval=settings.OPS_MONITOR_INTERVAL_SECONDS)  # fmt: skip
