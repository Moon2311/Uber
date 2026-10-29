from django.conf import settings
from django.core.management.base import BaseCommand

from apps.common.worker import run_loop
from apps.pricing.surge import prune_history, update_surge


class Command(BaseCommand):
    help = (
        "Recompute surge multipliers per zone (illustrative policy; see "
        "docs/pricing-payments.md for legal caveats). Long-running unless --once."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, once, **options):
        state = {"runs": 0}

        def step():
            zones = update_surge()
            state["runs"] += 1
            if state["runs"] % 1000 == 0:
                prune_history()
            if once:
                self.stdout.write(f"surge: {len(zones)} zone(s) updated")
            return 0  # always wait a full interval between passes

        run_loop("pricing.surge", step, interval=settings.PRICING_SURGE_INTERVAL_SECONDS,
                 once=once)  # fmt: skip
