"""Versioned rules, upfront quotes, surge (illustrative policy) and quote acceptance."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.pricing import surge
from apps.pricing.models import FareQuote, PricingRuleSet, RuleSetStatus, SurgeZone
from apps.pricing.rules import RuleError, default_rules, parse
from apps.pricing.services import (
    QuoteUnavailable,
    accept_quote,
    active_rule_set,
    publish_rule_set,
    quote_fare,
)
from apps.trips.services import cancel_trip_by_rider, request_trip
from conftest import SF, make_driver, make_rider, seed_location

pytestmark = pytest.mark.django_db
PICKUP = {"lat": SF["lat"], "lng": SF["lng"]}
DROPOFF = {"lat": Decimal("37.804400"), "lng": Decimal("-122.271200")}
D = Decimal


def quote(rider=None, **kw):
    return quote_fare(rider=rider or make_rider(), pickup=PICKUP, dropoff=DROPOFF,
                      ride_category=kw.pop("category", "ECONOMY"), **kw)  # fmt: skip


def rules_with(**surge_overrides):
    raw = default_rules()
    raw["surge"].update(surge_overrides)
    return raw


# --- rules ----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r["categories"].pop("XL"), "missing"),
        (lambda r: r["categories"]["ECONOMY"].update(per_km="-1"), "per_km"),
        (lambda r: r["categories"]["ECONOMY"].update(base="abc"), "base"),
        (lambda r: r.update(currency="usd"), "currency"),
        (lambda r: r["surge"].update(max_multiplier="9.0"), "HARD_CAP"),
        (lambda r: r["surge"].update(max_multiplier="0.5"), "max_multiplier"),
        (lambda r: r["surge"].update(window_seconds=0), "window"),
    ],
)
def test_invalid_rules_are_rejected(mutate, message):
    raw = default_rules()
    mutate(raw)
    with pytest.raises(RuleError, match=message):
        parse(raw)


def test_versions_are_immutable_and_quotes_keep_their_version():
    v1, _ = active_rule_set()
    old = quote()
    raw = default_rules()
    raw["categories"]["ECONOMY"]["base"] = "4.00"
    v2 = publish_rule_set(raw, notes="raise base")
    v1.refresh_from_db()
    assert (v1.status, v2.status, v2.version) == (RuleSetStatus.RETIRED, RuleSetStatus.ACTIVE, 2)
    assert PricingRuleSet.objects.filter(status=RuleSetStatus.ACTIVE).count() == 1
    new = quote()
    assert new.rule_set == v2 and new.amount == old.amount + D("1.50")
    old.refresh_from_db()
    assert old.rule_set == v1 and old.breakdown["pricing_version"] == 1
    v2.rules = default_rules()
    with pytest.raises(ValueError, match="immutable"):
        v2.save()


# --- estimate -------------------------------------------------------------------------------------
def test_quote_breakdown_adds_up_and_minimum_applies():
    q = quote()
    b = q.breakdown
    parts = D(b["base"]) + D(b["distance_charge"]) + D(b["time_charge"]) + D(b["booking_fee"])
    assert D(b["subtotal"]) == parts and q.base_amount == parts and q.amount == q.base_amount
    assert q.surge_multiplier == 1 and b["minimum_applied"] is False
    assert q.distance_km > 0 and q.duration_minutes > 0 and q.surge_zone
    short = quote_fare(rider=make_rider(), pickup=PICKUP,
                       dropoff={"lat": PICKUP["lat"] + D("0.0001"), "lng": PICKUP["lng"]},
                       ride_category="XL")  # fmt: skip
    assert short.amount == D("10.00") and short.breakdown["minimum_applied"] is True


def test_estimate_endpoint_returns_a_quote(rider_client):
    response = rider_client.post(reverse("v1:fare-estimate"), {
        "pickup": {"lat": "37.7749", "lng": "-122.4194"},
        "dropoff": {"lat": "37.8044", "lng": "-122.2712"}, "ride_category": "COMFORT",
    }, format="json")  # fmt: skip
    body = response.json()
    assert response.status_code == 200 and body["pricing_version"] == 1
    assert FareQuote.objects.filter(pk=body["quote_id"]).exists()
    assert body["surge_multiplier"] == "1.00" and body["breakdown"]["total"] == body["amount"]


# --- surge policy (pure) --------------------------------------------------------------------------
def test_surge_policy_bounds():
    p = parse(rules_with(min_demand=3, ratio_threshold="1.0", sensitivity="0.5",
                         max_multiplier="2.0", step="0.1")).surge  # fmt: skip
    assert p.target(demand=2, supply=0) == D("1.00")  # too little demand to mean anything
    assert p.target(demand=4, supply=4) == D("1.00")  # balanced
    assert p.target(demand=6, supply=2) == D("2.00")  # ratio 3 -> 1 + 0.5*2 = 2.0
    assert p.target(demand=5, supply=2) == D("1.70")  # 1.75 rounded DOWN to the step
    assert p.target(demand=100, supply=0) == D("2.00")  # capped
    assert p.smooth(D("1.0"), D("2.0")) == D("1.30")  # no jumps
    assert p.smooth(D("2.0"), D("1.0")) == D("1.70")


# --- surge zones (integration) --------------------------------------------------------------------
def test_demand_without_supply_raises_the_price_and_it_decays_back(settings):
    publish_rule_set(rules_with(min_demand=3, max_change_per_update="1.0"))
    for _ in range(4):
        quote(make_rider())  # 4 distinct riders, no drivers
    [zone] = surge.update_surge()
    assert zone.demand == 4 and zone.supply == 0 and zone.multiplier == D("2.00")
    q = quote(make_rider())
    assert q.surge_multiplier == D("2.00") and q.amount == (q.base_amount * 2).quantize(D("0.01"))
    assert q.breakdown["surge_amount"] == str(q.amount - q.base_amount)

    # Demand window passes and drivers arrive: the multiplier comes back down.
    FareQuote.objects.update(created_at=timezone.now() - timedelta(hours=1))
    [zone] = surge.update_surge()
    assert zone.multiplier == D("1.00")


def test_one_rider_refreshing_cannot_create_surge():
    rider = make_rider()
    for _ in range(20):
        quote(rider)
    zones = surge.update_surge()
    assert [z.demand for z in zones] == [1] and zones[0].multiplier == D("1.00")


def test_supply_counts_only_free_fresh_drivers_in_the_zone():
    publish_rule_set(rules_with(min_demand=3, ratio_threshold="1.0", max_change_per_update="1.0"))
    for _ in range(3):
        seed_location(make_driver(online=True), PICKUP["lat"], PICKUP["lng"])
    seed_location(make_driver(online=True), PICKUP["lat"], PICKUP["lng"], seconds_ago=3600)  # stale
    seed_location(make_driver(online=True), 40.7, -74.0)  # elsewhere
    for _ in range(3):
        quote(make_rider())
    [zone] = surge.update_surge()
    assert zone.supply == 3 and zone.multiplier == D("1.00")  # 3 riders, 3 drivers


@pytest.mark.parametrize("switch", ["global", "zone", "rules"])
def test_surge_can_be_switched_off(settings, switch):
    publish_rule_set(rules_with(min_demand=1, enabled=switch != "rules",
                                max_change_per_update="1.0"))  # fmt: skip
    for _ in range(3):
        quote(make_rider())
    [zone] = surge.update_surge()
    if switch == "global":
        settings.PRICING_SURGE_ENABLED = False
    elif switch == "zone":
        settings.PRICING_SURGE_DISABLED_ZONES = [zone.zone]
    assert quote(make_rider()).surge_multiplier == D("1.00")


def test_stale_surge_value_means_no_surge():
    _, rules = active_rule_set()
    zone = surge.zone_for(PICKUP["lat"], PICKUP["lng"])
    SurgeZone.objects.create(
        zone=zone,
        multiplier=D("1.8"),
        demand=9,
        supply=0,
        rule_set_version=1,
        computed_at=timezone.now() - timedelta(hours=1),
    )
    assert surge.current_multiplier(zone, rules.surge) == D("1.00")  # worker stopped


# --- the trip records the accepted quote ----------------------------------------------------------
def test_trip_uses_the_accepted_quote_and_records_version():
    rider = make_rider()
    q = quote(rider)
    trip = request_trip(rider=rider, pickup=PICKUP, dropoff=DROPOFF, ride_category="ECONOMY",
                        quote_id=q.pk)  # fmt: skip
    assert trip.fare_quote == q and trip.pricing_version == 1
    assert trip.estimated_fare == q.amount and trip.surge_multiplier == q.surge_multiplier
    assert trip.payment.amount == q.amount  # what will be charged

    cancel_trip_by_rider(trip_id=trip.pk, rider=rider)
    with pytest.raises(QuoteUnavailable, match="already used"):
        request_trip(rider=rider, pickup=PICKUP, dropoff=DROPOFF, ride_category="ECONOMY",
                     quote_id=q.pk)  # fmt: skip


def test_trip_without_quote_gets_a_fresh_one():
    trip = request_trip(rider=make_rider(), pickup=PICKUP, dropoff=DROPOFF, ride_category="XL")
    assert trip.fare_quote is not None and trip.fare_quote.ride_category == "XL"


@pytest.mark.parametrize("problem", ["expired", "other_rider", "category", "pickup"])
def test_quote_must_be_valid_for_this_trip(problem):
    rider = make_rider()
    q = quote(rider)
    kwargs = {"quote_id": q.pk, "rider": rider, "pickup": PICKUP, "dropoff": DROPOFF,
              "ride_category": "ECONOMY"}  # fmt: skip
    if problem == "expired":
        FareQuote.objects.filter(pk=q.pk).update(expires_at=timezone.now())
    elif problem == "other_rider":
        kwargs["rider"] = make_rider()
    elif problem == "category":
        kwargs["ride_category"] = "XL"
    else:
        kwargs["pickup"] = {"lat": PICKUP["lat"] + D("0.01"), "lng": PICKUP["lng"]}
    with pytest.raises(QuoteUnavailable):
        accept_quote(**kwargs)


def test_trip_api_accepts_quote_id(rider_client):
    body = {
        "pickup": {"lat": "37.7749", "lng": "-122.4194"},
        "dropoff": {"lat": "37.8044", "lng": "-122.2712"},
        "ride_category": "ECONOMY",
    }
    q = rider_client.post(reverse("v1:fare-estimate"), body, format="json").json()
    trip = rider_client.post(reverse("v1:trip-create"), {**body, "quote_id": q["quote_id"]},
                             format="json").json()  # fmt: skip
    assert trip["fare_quote"] == q["quote_id"] and trip["estimated_fare"] == q["amount"]
    assert trip["pricing_version"] == 1
