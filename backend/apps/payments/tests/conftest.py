import pytest

from apps.payments.models import PaymentRecord, SavedPaymentMethod
from apps.payments.providers.sandbox import SandboxProvider
from apps.trips.tests.conftest import S, advance, new_trip
from conftest import make_driver, make_rider


def card(user, token="tok_sandbox_visa", default=True) -> SavedPaymentMethod:
    details = SandboxProvider().describe_method(token)
    if default:
        SavedPaymentMethod.objects.filter(user=user).update(is_default=False)
    return SavedPaymentMethod.objects.create(
        user=user, provider="sandbox", provider_token=details.token, brand=details.brand,
        last4=details.last4, is_default=default,
    )  # fmt: skip


def completed_trip(token: str | None = "tok_sandbox_visa", rider=None):
    """A finished trip whose payment is due (card) or settled (cash)."""
    rider = rider or make_rider()
    if token:
        card(rider, token)
    trip = new_trip(rider)
    advance(trip, S.SEARCHING, S.DRIVER_ASSIGNED, S.DRIVER_ARRIVING, S.IN_PROGRESS, S.COMPLETED,
            driver=make_driver(online=True))  # fmt: skip
    return trip, PaymentRecord.objects.get(trip=trip)


@pytest.fixture
def provider():
    return SandboxProvider()
