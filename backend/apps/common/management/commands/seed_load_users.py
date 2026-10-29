"""Create load-test riders and drivers and write their access tokens to a file.

Refuses to run in staging/production. Users are tagged by email domain
(@load.invalid) so `--delete` removes exactly them.
"""

import json
import random

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from rest_framework_simplejwt.tokens import AccessToken

DOMAIN = "load.invalid"
CENTER = (37.7749, -122.4194)


class Command(BaseCommand):
    help = "Seed load-test users (development only)."

    def add_arguments(self, parser):
        parser.add_argument("--drivers", type=int, default=200)
        parser.add_argument("--riders", type=int, default=100)
        parser.add_argument("--out", default="/tmp/load-users.json")  # noqa: S108
        parser.add_argument("--delete", action="store_true", help="Remove load-test users")
        parser.add_argument("--reissue", action="store_true",
                            help="Only mint fresh tokens for existing load-test users")  # fmt: skip

    def handle(self, *args, drivers, riders, out, delete, reissue, **options):
        if settings.APP_ENV in ("staging", "production"):
            raise CommandError("Refusing to seed load-test users in a deployed environment")
        from apps.accounts.models import Role, User
        from apps.drivers.models import AvailabilityStatus, DriverProfile, Vehicle
        from apps.drivers.services import set_availability
        from apps.payments.models import SavedPaymentMethod
        from apps.payments.providers.sandbox import SandboxProvider

        if delete:
            self._delete()
            return
        if reissue:
            # The scheduler auto-offlines silent drivers (correct behaviour); bring the
            # load-test drivers back so every run starts from the same state.
            for d in DriverProfile.objects.filter(user__email__endswith=f"@{DOMAIN}",
                                                  availability_status="OFFLINE"):  # fmt: skip
                set_availability(driver=d, online=True)
            result = {
                "center": CENTER,
                "drivers": [
                    {"id": d.pk, "token": str(AccessToken.for_user(d.user))}
                    for d in DriverProfile.objects.filter(user__email__endswith=f"@{DOMAIN}")
                    .select_related("user")
                    .order_by("pk")
                ],
                "riders": [
                    {"id": u.pk, "token": str(AccessToken.for_user(u))}
                    for u in User.objects.filter(
                        email__endswith=f"@{DOMAIN}", role=Role.RIDER
                    ).order_by("pk")
                ],
            }
            with open(out, "w") as fh:
                json.dump(result, fh)
            self.stdout.write(f"Reissued tokens -> {out}")
            return
        rnd = random.Random(42)  # noqa: S311
        tag = rnd.getrandbits(32)
        result = {"drivers": [], "riders": [], "center": CENTER}
        with transaction.atomic():
            for i in range(drivers):
                user = User.objects.create_user(email=f"d{tag}-{i}@{DOMAIN}", password=None,
                                                first_name="Load", role=Role.DRIVER)  # fmt: skip
                profile = DriverProfile.objects.create(
                    user=user,
                    license_number=f"L{tag}-{i}",
                    availability_status=AvailabilityStatus.OFFLINE,
                )
                Vehicle.objects.create(driver=profile, make="Load", model="Test", color="Grey",
                                       plate_number=f"LD{tag % 10000:04d}{i:05d}", year=2022,
                                       category="ECONOMY")  # fmt: skip
                result["drivers"].append(
                    {"id": profile.pk, "token": str(AccessToken.for_user(user))}
                )
            sandbox = SandboxProvider()
            for i in range(riders):
                user = User.objects.create_user(
                    email=f"r{tag}-{i}@{DOMAIN}", password=None, first_name="Load", role=Role.RIDER
                )
                m = sandbox.describe_method("tok_sandbox_visa")
                SavedPaymentMethod.objects.create(user=user, provider="sandbox",
                                                  provider_token=m.token, brand=m.brand,
                                                  last4=m.last4, is_default=True)  # fmt: skip
                result["riders"].append({"id": user.pk, "token": str(AccessToken.for_user(user))})
        for d in DriverProfile.objects.filter(user__email__endswith=f"@{DOMAIN}",
                                              availability_status="OFFLINE"):  # fmt: skip
            set_availability(driver=d, online=True)
        with open(out, "w") as fh:
            json.dump(result, fh)
        self.stdout.write(f"Seeded {drivers} drivers, {riders} riders -> {out}")

    def _delete(self):
        from apps.accounts.models import User
        from apps.trips.models import Trip

        n = Trip.objects.filter(rider__email__endswith=f"@{DOMAIN}").count()
        self.stdout.write(f"Load-test data kept for inspection: {n} trips. Reset the dev "
                          "database (docker compose down -v) to remove it entirely.")  # fmt: skip
        User.objects.filter(email__endswith=f"@{DOMAIN}", trips_as_rider__isnull=True,
                            driver_profile__trips__isnull=True).delete()  # fmt: skip
