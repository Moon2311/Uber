from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.locations.service import get_location_service


class Command(BaseCommand):
    help = (
        "Remove expired drivers from the live index (cell sets, geo set, seen set). "
        "Reads never return expired drivers anyway; this reclaims memory. Schedule "
        "every minute alongside expire_stale_drivers."
    )

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(seconds=settings.LIVE_DRIVER_TTL_SECONDS)
        pruned = get_location_service().store.prune(older_than=cutoff)
        self.stdout.write(f"Pruned {pruned} expired live location(s).")
