"""Fare estimation behind a small interface so surge / ML pricing can plug in later."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

from apps.common.geo import haversine_km
from apps.drivers.models import RideCategory

CENTS = Decimal("0.01")
# Straight-line distance underestimates road distance; a routing engine replaces this.
ROAD_DISTANCE_FACTOR = Decimal("1.3")
AVERAGE_SPEED_KMH = Decimal("30")


@dataclass(frozen=True)
class Point:
    lat: Decimal
    lng: Decimal


@dataclass(frozen=True)
class FareEstimate:
    amount: Decimal
    currency: str
    distance_km: Decimal
    duration_minutes: int
    category: str


@dataclass(frozen=True)
class CategoryRate:
    base: Decimal
    per_km: Decimal
    per_minute: Decimal
    minimum: Decimal


DEFAULT_RATES: dict[str, CategoryRate] = {
    RideCategory.ECONOMY: CategoryRate(
        Decimal("2.50"), Decimal("1.10"), Decimal("0.20"), Decimal("5.00")
    ),
    RideCategory.COMFORT: CategoryRate(
        Decimal("3.50"), Decimal("1.50"), Decimal("0.30"), Decimal("8.00")
    ),
    RideCategory.XL: CategoryRate(
        Decimal("5.00"), Decimal("2.00"), Decimal("0.40"), Decimal("10.00")
    ),
}


class FareEstimator(ABC):
    @abstractmethod
    def estimate(self, *, pickup: Point, dropoff: Point, category: str) -> FareEstimate: ...


class DistanceTimeFareEstimator(FareEstimator):
    def __init__(self, rates: dict[str, CategoryRate] | None = None, currency: str | None = None):
        self.rates = rates or DEFAULT_RATES
        self.currency = currency or settings.FARE_CURRENCY

    def estimate(self, *, pickup: Point, dropoff: Point, category: str) -> FareEstimate:
        rate = self.rates[category]
        straight = Decimal(
            str(
                haversine_km(
                    float(pickup.lat), float(pickup.lng), float(dropoff.lat), float(dropoff.lng)
                )
            )
        )
        distance_km = (straight * ROAD_DISTANCE_FACTOR).quantize(Decimal("0.001"))
        minutes = max(
            1, int((distance_km / AVERAGE_SPEED_KMH * 60).to_integral_value(ROUND_HALF_UP))
        )
        amount = rate.base + rate.per_km * distance_km + rate.per_minute * minutes
        amount = max(amount, rate.minimum).quantize(CENTS, rounding=ROUND_HALF_UP)
        return FareEstimate(
            amount=amount,
            currency=self.currency,
            distance_km=distance_km,
            duration_minutes=minutes,
            category=category,
        )


def get_fare_estimator() -> FareEstimator:
    return DistanceTimeFareEstimator()
