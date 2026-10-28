import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.common.exceptions import ConflictError
from apps.common.metrics import metrics
from apps.locations.service import Freshness, get_location_service

from .models import AvailabilityStatus, DriverProfile, Vehicle

logger = logging.getLogger("apps.drivers")


def create_driver_profile(*, user, license_number: str, vehicle: dict) -> DriverProfile:
    profile = DriverProfile.objects.create(user=user, license_number=license_number.strip())
    Vehicle.objects.create(
        driver=profile,
        make=vehicle["make"],
        model=vehicle["model"],
        color=vehicle["color"],
        plate_number=vehicle["plate_number"].strip().upper(),
        year=vehicle["year"],
        category=vehicle["category"],
    )
    return profile


def _apply_status(driver: DriverProfile, target: str, *, reason: str) -> None:
    driver.availability_status = target
    driver.availability_changed_at = timezone.now()
    driver.save(update_fields=["availability_status", "availability_changed_at", "updated_at"])
    if target == AvailabilityStatus.OFFLINE:
        # Offline drivers' positions are not retained as "latest" (privacy) and
        # they must report a fresh fix after coming back online.
        get_location_service().clear(driver.pk)
    metrics().increment("driver.availability.changed", tags={"to": target, "reason": reason})
    logger.info(
        "driver.availability.changed",
        extra={"driver_id": driver.pk, "status": target, "reason": reason},
    )


@transaction.atomic
def set_availability(*, driver: DriverProfile, online: bool) -> DriverProfile:
    from apps.trips.models import Trip

    # Lock the row so concurrent toggles / location writes / trip assignment
    # all see a consistent state.
    driver = DriverProfile.objects.select_for_update().get(pk=driver.pk)
    target = AvailabilityStatus.ONLINE if online else AvailabilityStatus.OFFLINE

    if online and not driver.vehicles.filter(is_active=True).exists():
        raise ConflictError("An active vehicle is required to go online.")
    if not online and Trip.objects.active_for_driver(driver).exists():
        raise ConflictError("You cannot go offline during an active trip.")

    if driver.availability_status != target:
        _apply_status(driver, target, reason="driver_request")
    return driver


@dataclass(frozen=True)
class DriverAvailability:
    status: str
    location_freshness: Freshness
    last_location_at: object  # datetime | None

    @property
    def is_available(self) -> bool:
        """Matchable = ONLINE *and* has a fresh location."""
        return (
            self.status == AvailabilityStatus.ONLINE and self.location_freshness is Freshness.FRESH
        )


def get_driver_availability(driver: DriverProfile) -> DriverAvailability:
    service = get_location_service()
    fix = service.get_latest(driver.pk)
    return DriverAvailability(
        status=driver.availability_status,
        location_freshness=service.freshness(fix),
        last_location_at=fix.recorded_at if fix else None,
    )


def expire_stale_drivers(*, now=None) -> int:
    """Switch ONLINE drivers offline when their location has been silent too long.

    Drivers on an active trip are never switched off automatically (logged instead).
    Intended to run every minute (cron / Celery beat in a later step).
    """
    from apps.trips.models import Trip

    now = now or timezone.now()
    cutoff = now - timedelta(seconds=settings.DRIVER_AUTO_OFFLINE_SECONDS)
    service = get_location_service()

    # Grace period: a driver who just went online has not had time to report yet.
    candidate_ids = DriverProfile.objects.filter(
        Q(availability_changed_at__lt=cutoff) | Q(availability_changed_at__isnull=True),
        availability_status=AvailabilityStatus.ONLINE,
    ).values_list("pk", flat=True)

    expired = 0
    for driver_id in candidate_ids:
        fix = service.get_latest(driver_id)
        if fix is not None and fix.recorded_at >= cutoff:
            continue
        with transaction.atomic():
            driver = DriverProfile.objects.select_for_update().get(pk=driver_id)
            if driver.availability_status != AvailabilityStatus.ONLINE:
                continue
            if Trip.objects.active_for_driver(driver).exists():
                logger.warning("driver.stale_on_trip", extra={"driver_id": driver_id})
                metrics().increment("driver.stale_on_trip")
                continue
            # Re-check under the lock: a fix may have arrived meanwhile.
            fix = service.get_latest(driver_id)
            if fix is not None and fix.recorded_at >= cutoff:
                continue
            _apply_status(driver, AvailabilityStatus.OFFLINE, reason="stale_location")
            expired += 1
    metrics().gauge("driver.auto_offline.last_run", expired)
    return expired


@dataclass(frozen=True)
class NearbyDriver:
    public_id: str
    latitude: float
    longitude: float
    heading: float | None
    distance_km: float
    category: str


def find_nearby_drivers(
    *, lat: float, lng: float, radius_km: float, category: str | None = None, limit: int
) -> list[NearbyDriver]:
    """Available drivers near a point, nearest first.

    Location filtering (radius + freshness) is delegated to the location service;
    availability (ONLINE + active vehicle + category) is decided here, in the
    drivers domain. H3 + Redis replace the geo part in a later step.
    """
    nearby = get_location_service().find_nearby(lat=lat, lng=lng, radius_km=radius_km)
    if not nearby:
        return []

    vehicle_filter = {
        "driver_id__in": [fix.driver_id for fix, _ in nearby],
        "is_active": True,
        "driver__availability_status": AvailabilityStatus.ONLINE,
    }
    if category:
        vehicle_filter["category"] = category
    eligible = {
        driver_id: (public_id, vehicle_category)
        for driver_id, public_id, vehicle_category in Vehicle.objects.filter(
            **vehicle_filter
        ).values_list("driver_id", "driver__public_id", "category")
    }

    results = []
    for fix, distance in nearby:
        if fix.driver_id not in eligible:
            continue
        public_id, vehicle_category = eligible[fix.driver_id]
        results.append(
            NearbyDriver(
                public_id=str(public_id),
                latitude=float(fix.latitude),
                longitude=float(fix.longitude),
                heading=fix.heading,
                distance_km=round(distance, 3),
                category=vehicle_category,
            )
        )
        if len(results) >= limit:
            break
    metrics().increment("drivers.nearby.search")
    metrics().gauge("drivers.nearby.results", len(results))
    return results
