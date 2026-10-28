from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.locations.models import DriverLocationEvent


class Command(BaseCommand):
    help = "Delete sampled location history older than the retention window."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=None, help="Override LOCATION_HISTORY_RETENTION_DAYS."
        )
        parser.add_argument("--batch-size", type=int, default=5000)

    def handle(self, *args, days, batch_size, **options):
        days = days if days is not None else settings.LOCATION_HISTORY_RETENTION_DAYS
        cutoff = timezone.now() - timedelta(days=days)
        total = 0
        while True:  # batched to keep transactions and locks short
            ids = list(
                DriverLocationEvent.objects.filter(recorded_at__lt=cutoff).values_list(
                    "id", flat=True
                )[:batch_size]
            )
            if not ids:
                break
            total += DriverLocationEvent.objects.filter(id__in=ids).delete()[0]
        self.stdout.write(f"Deleted {total} location events older than {days} days.")
