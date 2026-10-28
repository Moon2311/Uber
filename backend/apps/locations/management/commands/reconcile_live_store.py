from django.core.management.base import BaseCommand, CommandError

from apps.locations.failover import FailoverLocationStore
from apps.locations.service import build_store
from apps.locations.stores import StoreUnavailable


class Command(BaseCommand):
    help = (
        "Bring Redis back in line with PostgreSQL after an outage or data loss: replay "
        "fixes written to the PostgreSQL fallback, and if Redis lost its keyspace re-push "
        "every driver's availability. Idempotent. Run at startup and every minute."
    )

    def handle(self, *args, **options):
        store = build_store()
        if not isinstance(store, FailoverLocationStore):
            if hasattr(store, "ensure_epoch") and store.ensure_epoch():
                from apps.drivers.services import resync_live_availability

                stats = resync_live_availability(changed_since=None)
                self.stdout.write(f"Redis keyspace was empty; resynced {stats['synced']}.")
            else:
                self.stdout.write("Nothing to reconcile.")
            return
        try:
            result = store.reconcile()
        except StoreUnavailable as exc:
            raise CommandError(f"Redis unavailable, cannot reconcile yet: {exc}") from exc
        self.stdout.write(f"Reconcile: {result['action']}, replayed {result['replayed']} fix(es).")
