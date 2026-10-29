"""Pricing rules: the parsed, validated form of a PricingRuleSet's JSON.

Rules are data, not code: a new version is published (see services.publish_rule_set),
never edited in place, so every quote can be explained by the exact rules that
produced it.

Surge here is an ILLUSTRATIVE policy (demand/supply ratio -> bounded multiplier).
Dynamic pricing is regulated in many places (price caps, emergency price-gouging
rules, disclosure and consent requirements, bans for some services). Operators
must configure, cap or disable it per jurisdiction and obtain legal review before
using it with real riders. See docs/pricing-payments.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from django.conf import settings

from apps.drivers.models import RideCategory


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class CategoryRate:
    base: Decimal
    per_km: Decimal
    per_minute: Decimal
    minimum: Decimal
    booking_fee: Decimal = Decimal("0")


@dataclass(frozen=True)
class SurgePolicy:
    enabled: bool
    min_demand: int  # below this many distinct riders in a zone: no surge
    ratio_threshold: Decimal  # demand/supply above which surge starts
    sensitivity: Decimal  # multiplier increase per unit of ratio above threshold
    max_multiplier: Decimal
    step: Decimal  # multipliers are rounded DOWN to this step (e.g. 0.1)
    max_change_per_update: Decimal  # smoothing: no jumps between updates
    window_seconds: int  # demand look-back
    stale_after_seconds: int  # a zone value older than this counts as 1.0

    def target(self, demand: int, supply: int) -> Decimal:
        """Illustrative: 1.0 until demand is meaningful and exceeds supply by
        `ratio_threshold`, then linear in the ratio, capped."""
        if not self.enabled or demand < self.min_demand:
            return Decimal("1.0")
        ratio = Decimal(demand) / Decimal(max(supply, 1))
        raw = Decimal("1") + self.sensitivity * max(Decimal("0"), ratio - self.ratio_threshold)
        return self.clamp(raw)

    def smooth(self, previous: Decimal, target: Decimal) -> Decimal:
        delta = max(-self.max_change_per_update, min(self.max_change_per_update, target - previous))
        return self.clamp(previous + delta)

    def clamp(self, value: Decimal) -> Decimal:
        value = max(Decimal("1.0"), min(self.max_multiplier, value))
        steps = (value / self.step).to_integral_value(rounding=ROUND_FLOOR)
        return (steps * self.step).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class PricingRules:
    currency: str
    categories: dict[str, CategoryRate]
    road_factor: Decimal  # straight line -> road distance (routing engine replaces this)
    average_speed_kmh: Decimal
    surge: SurgePolicy


DEFAULT_RULES: dict = {
    "currency": None,  # filled from settings.FARE_CURRENCY
    "categories": {
        "ECONOMY": {"base": "2.50", "per_km": "1.10", "per_minute": "0.20", "minimum": "5.00"},
        "COMFORT": {"base": "3.50", "per_km": "1.50", "per_minute": "0.30", "minimum": "8.00"},
        "XL": {"base": "5.00", "per_km": "2.00", "per_minute": "0.40", "minimum": "10.00"},
    },
    "distance": {"road_factor": "1.3", "average_speed_kmh": "30"},
    "surge": {
        "enabled": True,
        "min_demand": 3,
        "ratio_threshold": "1.2",
        "sensitivity": "0.5",
        "max_multiplier": "2.0",
        "step": "0.1",
        "max_change_per_update": "0.3",
        "window_seconds": 600,
        "stale_after_seconds": 300,
    },
}


def default_rules() -> dict:
    import copy

    rules = copy.deepcopy(DEFAULT_RULES)
    rules["currency"] = settings.FARE_CURRENCY
    return rules


def _money(value, where: str, *, positive=False) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise RuleError(f"{where}: not a decimal") from exc
    if not d.is_finite() or d < 0 or (positive and d == 0):
        raise RuleError(f"{where}: must be {'>' if positive else '>='} 0")
    return d


def parse(raw: dict) -> PricingRules:
    """Validate a rules document. Raises RuleError with the offending path."""
    if not isinstance(raw, dict):
        raise RuleError("rules must be an object")
    currency = raw.get("currency")
    if not (isinstance(currency, str) and len(currency) == 3 and currency.isupper()):
        raise RuleError("currency: ISO-4217 code expected")
    cats = raw.get("categories") or {}
    missing = set(RideCategory.values) - set(cats)
    if missing:
        raise RuleError(f"categories: missing {sorted(missing)}")
    categories = {}
    for name, c in cats.items():
        if name not in RideCategory.values:
            raise RuleError(f"categories.{name}: unknown ride category")
        categories[name] = CategoryRate(
            base=_money(c.get("base"), f"categories.{name}.base"),
            per_km=_money(c.get("per_km"), f"categories.{name}.per_km"),
            per_minute=_money(c.get("per_minute"), f"categories.{name}.per_minute"),
            minimum=_money(c.get("minimum"), f"categories.{name}.minimum"),
            booking_fee=_money(c.get("booking_fee", "0"), f"categories.{name}.booking_fee"),
        )
    dist = raw.get("distance") or {}
    s = raw.get("surge") or {}
    hard_cap = Decimal(str(settings.PRICING_SURGE_HARD_CAP))
    try:
        surge = SurgePolicy(
            enabled=bool(s.get("enabled", False)),
            min_demand=int(s.get("min_demand", 3)),
            ratio_threshold=_money(s.get("ratio_threshold", "1.2"), "surge.ratio_threshold"),
            sensitivity=_money(s.get("sensitivity", "0.5"), "surge.sensitivity"),
            max_multiplier=_money(
                s.get("max_multiplier", "1.0"), "surge.max_multiplier", positive=True
            ),  # fmt: skip
            step=_money(s.get("step", "0.1"), "surge.step", positive=True),
            max_change_per_update=_money(
                s.get("max_change_per_update", "0.3"), "surge.max_change_per_update", positive=True
            ),  # fmt: skip
            window_seconds=int(s.get("window_seconds", 600)),
            stale_after_seconds=int(s.get("stale_after_seconds", 300)),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, RuleError):
            raise
        raise RuleError(f"surge: {exc}") from exc
    if surge.max_multiplier < 1:
        raise RuleError("surge.max_multiplier: must be >= 1.0")
    if surge.max_multiplier > hard_cap:
        raise RuleError(f"surge.max_multiplier: exceeds PRICING_SURGE_HARD_CAP ({hard_cap})")
    if surge.window_seconds <= 0 or surge.stale_after_seconds <= 0 or surge.min_demand < 1:
        raise RuleError("surge: window/staleness must be > 0 and min_demand >= 1")
    return PricingRules(
        currency=currency,
        categories=categories,
        road_factor=_money(dist.get("road_factor", "1.3"), "distance.road_factor", positive=True),
        average_speed_kmh=_money(
            dist.get("average_speed_kmh", "30"), "distance.average_speed_kmh", positive=True
        ),  # fmt: skip
        surge=surge,
    )
