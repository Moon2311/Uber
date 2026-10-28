from django.core.management.base import BaseCommand

from apps.geoindex.service import get_geo_search_service
from apps.locations.service import get_location_service


class Command(BaseCommand):
    help = (
        "Recompute the H3 cell of every live driver location (Redis and/or PostgreSQL) "
        "at the configured H3_RESOLUTION. Run after changing the resolution."
    )

    def handle(self, *args, **options):
        geo = get_geo_search_service()

        def cell_fn(lat, lng):
            return geo.cell_for(lat, lng), geo.resolution

        updated, failed = get_location_service().store.reindex_cells(cell_fn)
        self.stdout.write(
            f"Reindexed {updated} location(s) at resolution {geo.resolution}; {failed} failed."
        )
