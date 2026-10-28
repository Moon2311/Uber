from django.core.management.base import BaseCommand

from apps.drivers.services import expire_stale_drivers


class Command(BaseCommand):
    help = "Switch ONLINE drivers offline when their location has gone silent."

    def handle(self, *args, **options):
        count = expire_stale_drivers()
        self.stdout.write(f"Switched {count} stale driver(s) offline.")
