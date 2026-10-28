from django.core.management.base import BaseCommand

from apps.geoindex.h3_index import GeoIndexError
from apps.geoindex.service import get_geo_search_service
from apps.locations.models import DriverLocation


class Command(BaseCommand):
    help = (
        "Recompute the H3 cell of every latest driver location at the configured "
        "H3_RESOLUTION. Run after changing the resolution."
    )

    def handle(self, *args, **options):
        geo = get_geo_search_service()
        stale = DriverLocation.objects.exclude(h3_resolution=geo.resolution) | (
            DriverLocation.objects.filter(h3_cell__isnull=True)
        )
        updated = failed = 0
        for row in stale.iterator():
            try:
                cell = geo.cell_for(float(row.latitude), float(row.longitude))
            except GeoIndexError:
                failed += 1
                continue
            # Conditional update: don't clobber a newer fix written meanwhile.
            updated += DriverLocation.objects.filter(pk=row.pk, recorded_at=row.recorded_at).update(
                h3_cell=cell, h3_resolution=geo.resolution
            )
        self.stdout.write(
            f"Reindexed {updated} location(s) at resolution {geo.resolution}; {failed} failed."
        )
