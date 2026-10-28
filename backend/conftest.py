import itertools
from decimal import Decimal

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.accounts.models import Role, User
from apps.common import metrics as metrics_module
from apps.drivers.models import AvailabilityStatus, DriverProfile, RideCategory, Vehicle

PASSWORD = "S3cure-Passw0rd!"
_seq = itertools.count(1)


@pytest.fixture(autouse=True)
def _isolate(settings):
    cache.clear()  # throttling counters live in the cache
    settings.METRICS_BACKEND = "apps.common.metrics.InMemoryMetrics"
    metrics_module.metrics.cache_clear()
    yield
    metrics_module.metrics.cache_clear()


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
