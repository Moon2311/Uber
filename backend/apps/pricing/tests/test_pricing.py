from decimal import Decimal

import pytest
from django.urls import reverse

from apps.pricing.services import DistanceTimeFareEstimator, Point


def test_minimum_fare_applies_for_short_trips(settings):
    estimate = DistanceTimeFareEstimator(currency="USD").estimate(
        pickup=Point(Decimal("0"), Decimal("0")),
        dropoff=Point(Decimal("0.0001"), Decimal("0")),
        category="ECONOMY",
    )
    assert estimate.amount == Decimal("5.00")


def test_comfort_costs_more_than_economy():
    est = DistanceTimeFareEstimator(currency="USD")
    a, b = Point(Decimal("37.77"), Decimal("-122.41")), Point(Decimal("37.80"), Decimal("-122.27"))
    assert (
        est.estimate(pickup=a, dropoff=b, category="COMFORT").amount
        > est.estimate(pickup=a, dropoff=b, category="ECONOMY").amount
    )


@pytest.mark.django_db
def test_estimate_endpoint(rider_client):
    response = rider_client.post(
        reverse("v1:fare-estimate"),
        {"pickup": {"lat": 1, "lng": 1}, "dropoff": {"lat": 1.05, "lng": 1}, "ride_category": "XL"},
        format="json",
    )
    assert response.status_code == 200 and response.json()["currency"] == "USD"
