import itertools
import os
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.common import metrics as metrics_module
from apps.drivers.models import AvailabilityStatus, DriverProfile, RideCategory, Vehicle

PASSWORD = "S3cure-Passw0rd!"
_seq = itertools.count(1)


TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6380/15")


@pytest.fixture(autouse=True)
def _isolate(settings):
    cache.clear()  # throttling counters live in the cache
    settings.METRICS_BACKEND = "apps.common.metrics.InMemoryMetrics"
    # Production uses PBKDF2; tests only need *a* hash and create many users.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    # Unless a test opts into Redis (live_store_backend / redis_store fixtures),
    # live locations use PostgreSQL. Redis keys are namespaced per test.
    settings.LOCATION_STORE = "postgres"
    settings.REDIS_URL = TEST_REDIS_URL
    settings.REDIS_KEY_PREFIX = f"test-{uuid.uuid4().hex[:12]}"
    metrics_module.metrics.cache_clear()
    yield
    metrics_module.metrics.cache_clear()
    _delete_redis_keys(settings.REDIS_KEY_PREFIX)


def _delete_redis_keys(prefix):
    import redis

    from apps.common.redis import get_redis

    try:
        client = get_redis()
        keys = list(client.scan_iter(match=f"{prefix}:*", count=500))
        if keys:
            client.delete(*keys)
    except redis.exceptions.RedisError:
        pass


@pytest.fixture
def redis_client(settings):
    from apps.common.redis import get_redis

    client = get_redis()
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"Redis tests need Redis at {settings.REDIS_URL} (make db): {exc}")
    return client


@pytest.fixture(params=["postgres", "redis"])
def live_store_backend(request, settings):
    """Run a test against each live-store implementation. Redis runs WITHOUT the
    PostgreSQL fallback so a Redis bug cannot hide behind it."""
    settings.LOCATION_STORE = request.param
    settings.LOCATION_STORE_FALLBACK = "none"
    if request.param == "redis":
        request.getfixturevalue("redis_client")
    return request.param


def live_store():
    from apps.locations.service import get_location_service

    return get_location_service().store


def latest(driver):
    from apps.locations.service import get_location_service

    return get_location_service().get_latest(driver.pk)


def seed_location(driver, lat, lng, *, seconds_ago=0, index=True):
    """Put a fix straight into the active store (bypasses API age checks)."""
    from apps.geoindex.service import get_geo_search_service
    from apps.locations.stores import LocationFix

    store = live_store()
    driver.refresh_from_db(fields=["availability_status"])
    category = driver.vehicles.filter(is_active=True).values_list("category", flat=True).first()
    store.set_availability(driver.pk, online=driver.is_online, category=category)
    geo = get_geo_search_service()
    return store.upsert_if_newer(
        LocationFix(
            driver_id=driver.pk,
            latitude=Decimal(f"{float(lat):.6f}"),
            longitude=Decimal(f"{float(lng):.6f}"),
            recorded_at=timezone.now() - timedelta(seconds=seconds_ago),
            h3_cell=geo.cell_for(lat, lng) if index else None,
            h3_resolution=geo.resolution if index else None,
        )
    )


@pytest.fixture
def recorded_metrics():
    return metrics_module.metrics()


@pytest.fixture
def api_client():
    return APIClient()


def make_rider(**kwargs) -> User:
    n = next(_seq)
    return User.objects.create_user(
        email=kwargs.pop("email", f"rider{n}@example.com"),
        password=PASSWORD,
        first_name="Rita",
        role=Role.RIDER,
        **kwargs,
    )


def make_driver(*, online=False, category=RideCategory.ECONOMY) -> DriverProfile:
    n = next(_seq)
    user = User.objects.create_user(
        email=f"driver{n}@example.com", password=PASSWORD, first_name="Dan", role=Role.DRIVER
    )
    profile = DriverProfile.objects.create(
        user=user,
        license_number=f"LIC-{n}",
        availability_status=AvailabilityStatus.ONLINE if online else AvailabilityStatus.OFFLINE,
    )
    Vehicle.objects.create(
        driver=profile,
        make="Toyota",
        model="Prius",
        color="White",
        plate_number=f"ABC{n:04d}",
        year=2020,
        category=category,
    )
    return profile


@pytest.fixture
def rider(db):
    return make_rider()


@pytest.fixture
def driver(db):
    return make_driver(online=True)


@pytest.fixture
def offline_driver(db):
    return make_driver(online=False)


def client_for(user) -> APIClient:
    from rest_framework_simplejwt.tokens import RefreshToken

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
    return client


@pytest.fixture
def rider_client(rider):
    return client_for(rider)


@pytest.fixture
def driver_client(driver):
    return client_for(driver.user)


SF = {"lat": Decimal("37.774900"), "lng": Decimal("-122.419400")}
