from decimal import Decimal

import pytest

from apps.drivers.models import DriverProfile
from apps.matching.eta import ResilientEtaService
from apps.matching.orchestrator import MatchingOrchestrator
from apps.matching.state import DriverStateStore, NullReservationLocks, RedisReservationLocks
from apps.trips.services import request_trip
from conftest import make_driver, make_rider, seed_location

PICKUP = (37.7749, -122.4194)
DROPOFF = (37.8044, -122.2712)


@pytest.fixture
def matching_env(settings, redis_client):
    """Live index in Redis (H3 cell sets), Redis reservation locks, labelled fallback ETA."""
    settings.LOCATION_STORE = "redis"
    settings.LOCATION_STORE_FALLBACK = "none"
    settings.MATCHING_RESERVATION_LOCKS = "redis"
    settings.ETA_PROVIDER = "fallback"
    return settings


def driver_at(lat, lng, *, category="ECONOMY", rating="5.00", seconds_ago=0):
    d = make_driver(online=True, category=category)
    if rating != "5.00":
        DriverProfile.objects.filter(pk=d.pk).update(rating=Decimal(rating))
    seed_location(d, lat, lng, seconds_ago=seconds_ago)
    return d


def north(km):
    """Point `km` north of the pickup."""
    return (PICKUP[0] + km / 111.32, PICKUP[1])


def new_trip(rider=None, category="ECONOMY", pickup=PICKUP):
    return request_trip(
        rider=rider or make_rider(),
        pickup={"lat": Decimal(f"{pickup[0]:.6f}"), "lng": Decimal(f"{pickup[1]:.6f}")},
        dropoff={"lat": Decimal(f"{DROPOFF[0]:.6f}"), "lng": Decimal(f"{DROPOFF[1]:.6f}")},
        ride_category=category,
    )


def orchestrator(locks="redis", **kw):
    lock_impl = RedisReservationLocks() if locks == "redis" else NullReservationLocks()
    return MatchingOrchestrator(
        state=DriverStateStore(locks=lock_impl), eta=ResilientEtaService(None), **kw
    )
