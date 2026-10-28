from django.core.management.base import BaseCommand

from apps.drivers.services import resync_live_availability
from apps.geoindex.service import get_geo_search_service
from apps.locations.service import get_location_service


class Command(BaseCommand):
    help = (
        "Rebuild the live index from PostgreSQL after Redis data loss or drift: "
        "re-push every driver's availability, then recompute H3 cells. Live positions "
        "themselves are not in PostgreSQL; they reappear with each driver's next GPS fix."
    )

    def handle(self, *args, **options):
        stats = resync_live_availability(changed_since=None)
        geo = get_geo_search_service()
        updated, failed = get_location_service().store.reindex_cells(
            lambda lat, lng: (geo.cell_for(lat, lng), geo.resolution)
        )
        self.stdout.write(
            f"Availability re-pushed for {stats['synced']} driver(s); "
            f"{updated} cell(s) reindexed, {failed} failed."
        )
