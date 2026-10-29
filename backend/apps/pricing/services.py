"""Fare estimation and upfront quotes.

    quote_fare()   distance/duration + versioned rules + zone surge -> FareQuote
    accept_quote() the rider's quote, still valid and matching the request, is
                   locked for the trip being created (one quote, one trip)

The estimator itself is pure (no DB) so it is easy to test and to swap for a
routing-engine-based one.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.common.exceptions import ConflictError
from apps.common.geo import haversine_km
from apps.common.metrics import metrics

from . import rules as rules_module
from .models import FareQuote, PricingRuleSet, RuleSetStatus
from .rules import CategoryRate, PricingRules

logger = logging.getLogger("apps.pricing")
CENTS = Decimal("0.01")


@dataclass(frozen=True)
class Point:
    lat: Decimal
    lng: Decimal


@dataclass(frozen=True)
class FareEstimate:
    amount: Decimal  # before surge
    currency: str
    distance_km: Decimal
    duration_minutes: int
    category: str
    breakdown: dict | None = None


class FareEstimator(ABC):
    @abstractmethod
    def estimate(self, *, pickup: Point, dropoff: Point, category: str) -> FareEstimate: ...


class DistanceTimeFareEstimator(FareEstimator):
    """base + per_km * road_km + per_minute * minutes + booking_fee, at least `minimum`."""

    def __init__(self, rules: PricingRules | None = None, currency: str | None = None):
        self.rules = rules or rules_module.parse(rules_module.default_rules())
        self.currency = currency or self.rules.currency

    def estimate(self, *, pickup: Point, dropoff: Point, category: str) -> FareEstimate:
        rate: CategoryRate = self.rules.categories[category]
        straight = Decimal(
            str(haversine_km(float(pickup.lat), float(pickup.lng), float(dropoff.lat),
                             float(dropoff.lng)))
        )  # fmt: skip
        distance_km = (straight * self.rules.road_factor).quantize(Decimal("0.001"))
        minutes = max(
            1,
            int((distance_km / self.rules.average_speed_kmh * 60).to_integral_value(ROUND_HALF_UP)),
        )
        distance_charge = (rate.per_km * distance_km).quantize(CENTS, ROUND_HALF_UP)
        time_charge = (rate.per_minute * minutes).quantize(CENTS, ROUND_HALF_UP)
        subtotal = rate.base + distance_charge + time_charge + rate.booking_fee
        amount = max(subtotal, rate.minimum).quantize(CENTS, ROUND_HALF_UP)
        breakdown = {
            "base": str(rate.base), "distance_charge": str(distance_charge),
            "time_charge": str(time_charge), "booking_fee": str(rate.booking_fee),
            "subtotal": str(subtotal.quantize(CENTS)), "minimum": str(rate.minimum),
            "minimum_applied": subtotal < rate.minimum,
        }  # fmt: skip
        return FareEstimate(amount, self.currency, distance_km, minutes, category, breakdown)


def get_fare_estimator() -> FareEstimator:
    return DistanceTimeFareEstimator(active_rule_set()[1])


# --- versioned rules -----------------------------------------------------------------
def active_rule_set() -> tuple[PricingRuleSet, PricingRules]:
    """The ACTIVE version. A fresh database is bootstrapped with the built-in
    defaults as version 1 (so tests and new environments work without seeding)."""
    row = PricingRuleSet.objects.filter(status=RuleSetStatus.ACTIVE).first()
    if row is None:
        try:
            with transaction.atomic():
                row = PricingRuleSet.objects.create(
                    version=1, status=RuleSetStatus.ACTIVE, rules=rules_module.default_rules(),
                    notes="built-in defaults", activated_at=timezone.now(),
                )  # fmt: skip
        except IntegrityError:  # a concurrent bootstrap won
            row = PricingRuleSet.objects.get(status=RuleSetStatus.ACTIVE)
    return row, rules_module.parse(row.rules)


@transaction.atomic
def publish_rule_set(raw: dict, *, notes: str = "", user=None) -> PricingRuleSet:
    """Validate and activate a NEW version; the previous one is retired, never edited.
    Quotes already issued keep pointing at the version that priced them."""
    rules_module.parse(raw)  # raises RuleError
    current = PricingRuleSet.objects.select_for_update().filter(status=RuleSetStatus.ACTIVE)
    now = timezone.now()
    last = PricingRuleSet.objects.order_by("-version").values_list("version", flat=True).first()
    for row in current:
        row.status, row.retired_at = RuleSetStatus.RETIRED, now
        row.save(update_fields=["status", "retired_at"])
    created = PricingRuleSet.objects.create(
        version=(last or 0) + 1, status=RuleSetStatus.ACTIVE, rules=raw, notes=notes,
        created_by=user, activated_at=now,
    )  # fmt: skip
    logger.info("pricing.rules.published", extra={"version": created.version})
    return created


# --- quotes ------------------------------------------------------------------------------
class QuoteUnavailable(ConflictError):
    default_code = "quote_unavailable"


def quote_fare(*, rider, pickup: dict, dropoff: dict, ride_category: str, now=None) -> FareQuote:
    from .surge import current_multiplier, zone_for

    now = now or timezone.now()
    rule_set, rules = active_rule_set()
    estimate = DistanceTimeFareEstimator(rules).estimate(
        pickup=Point(pickup["lat"], pickup["lng"]),
        dropoff=Point(dropoff["lat"], dropoff["lng"]),
        category=ride_category,
    )
    zone = zone_for(pickup["lat"], pickup["lng"])
    multiplier = current_multiplier(zone, rules.surge, now=now)
    amount = (estimate.amount * multiplier).quantize(CENTS, ROUND_HALF_UP)
    breakdown = {
        **estimate.breakdown, "pre_surge_total": str(estimate.amount),
        "surge_multiplier": str(multiplier), "surge_amount": str(amount - estimate.amount),
        "total": str(amount), "pricing_version": rule_set.version,
    }  # fmt: skip
    quote = FareQuote.objects.create(
        rider=rider, rule_set=rule_set, ride_category=ride_category,
        pickup_latitude=pickup["lat"], pickup_longitude=pickup["lng"],
        dropoff_latitude=dropoff["lat"], dropoff_longitude=dropoff["lng"],
        distance_km=estimate.distance_km, duration_minutes=estimate.duration_minutes,
        surge_zone=zone or "", surge_multiplier=multiplier, base_amount=estimate.amount,
        amount=amount, currency=rules.currency, breakdown=breakdown,
        expires_at=now + timedelta(seconds=settings.PRICING_QUOTE_TTL_SECONDS),
    )  # fmt: skip
    metrics().increment("pricing.quote.created", tags={"surge": str(multiplier > 1).lower()})
    return quote


def _same_point(quote_lat, quote_lng, point) -> bool:
    q = Decimal("0.000001")
    return (Decimal(str(point["lat"])).quantize(q), Decimal(str(point["lng"])).quantize(q)) == (
        quote_lat.quantize(q), quote_lng.quantize(q),
    )  # fmt: skip


def accept_quote(*, quote_id, rider, pickup: dict, dropoff: dict, ride_category: str,
                 now=None) -> FareQuote:  # fmt: skip
    """Lock the rider's quote for trip creation (caller's transaction). The trip's
    OneToOne on fare_quote guarantees it is used once."""
    now = now or timezone.now()
    quote = FareQuote.objects.select_for_update().filter(pk=quote_id, rider=rider).first()
    if quote is None:
        raise QuoteUnavailable("Unknown fare quote.")
    if hasattr(quote, "trip"):
        raise QuoteUnavailable("This fare quote was already used.")
    if quote.expires_at <= now:
        raise QuoteUnavailable("This fare quote has expired; request a new one.")
    if (
        quote.ride_category != ride_category
        or not _same_point(quote.pickup_latitude, quote.pickup_longitude, pickup)
        or not _same_point(quote.dropoff_latitude, quote.dropoff_longitude, dropoff)
    ):
        raise QuoteUnavailable("This fare quote is for a different trip.")
    return quote
