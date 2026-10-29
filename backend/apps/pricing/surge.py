"""Surge zones: demand (distinct riders asking for a price) vs supply (free drivers).

ILLUSTRATIVE POLICY. It must be reviewed against local law before use with real
riders: many jurisdictions cap or forbid dynamic pricing (especially during
emergencies) or require it to be disclosed and accepted. Operators can switch it
off globally (PRICING_SURGE_ENABLED=false), per rule version (surge.enabled), or
for specific zones (PRICING_SURGE_DISABLED_ZONES), and PRICING_SURGE_HARD_CAP
bounds any rule set.

Design choices that keep it predictable:
  * computed periodically (update_surge), not per request, so a quote is a cheap
    lookup and every multiplier is recorded with its inputs (SurgeZone rows);
  * demand counts DISTINCT riders, so one client refreshing the price screen
    cannot raise the price;
  * smoothing limits the change per update, values are rounded down to a step,
    capped, and a stale value (worker stopped) means NO surge, never a stuck one.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from functools import lru_cache

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from apps.common.metrics import metrics
from apps.geoindex.h3_index import GeoIndexError, H3GeoIndex

from .models import FareQuote, SurgeZone
from .rules import SurgePolicy

logger = logging.getLogger("apps.pricing")
ONE = Decimal("1.00")


@lru_cache(maxsize=4)
def _zone_index(resolution: int) -> H3GeoIndex:
    return H3GeoIndex(resolution)


def zone_index() -> H3GeoIndex:
    return _zone_index(settings.PRICING_SURGE_H3_RESOLUTION)


def zone_for(lat, lng) -> str | None:
    try:
        return zone_index().cell_for(float(lat), float(lng))
    except GeoIndexError:
        return None


def _surge_allowed(zone: str | None, policy: SurgePolicy) -> bool:
    return bool(
        zone
        and settings.PRICING_SURGE_ENABLED
        and policy.enabled
        and zone not in settings.PRICING_SURGE_DISABLED_ZONES
    )


def current_multiplier(zone: str | None, policy: SurgePolicy, *, now=None) -> Decimal:
    if not _surge_allowed(zone, policy):
        return ONE
    now = now or timezone.now()
    row = (
        SurgeZone.objects.filter(
            zone=zone, computed_at__gte=now - timedelta(seconds=policy.stale_after_seconds)
        )
        .order_by("-computed_at")
        .first()
    )
    return policy.clamp(row.multiplier) if row else ONE


def free_supply(zone: str, *, now=None) -> int:
    """Online drivers with a fresh fix inside the zone and no active trip."""
    from apps.geoindex.service import get_geo_search_service
    from apps.locations.service import get_location_service
    from apps.trips.models import DRIVER_ACTIVE_STATUSES, Trip

    now = now or timezone.now()
    cells = zone_index().children(zone, get_geo_search_service().resolution)
    fresh_after = now - timedelta(seconds=settings.DRIVER_LOCATION_STALE_SECONDS)
    fixes = get_location_service().store.find_in_cells(cells, fresh_after=fresh_after)
    ids = {f.driver_id for f in fixes}
    if not ids:
        return 0
    busy = set(
        Trip.objects.filter(driver_id__in=ids, status__in=DRIVER_ACTIVE_STATUSES).values_list(
            "driver_id", flat=True
        )
    )
    return len(ids - busy)


def update_surge(*, now=None) -> list[SurgeZone]:
    """One pass over every zone with recent demand or a live multiplier."""
    from .services import active_rule_set

    now = now or timezone.now()
    rule_set, rules = active_rule_set()
    policy = rules.surge
    window_start = now - timedelta(seconds=policy.window_seconds)
    demand = dict(
        FareQuote.objects.filter(created_at__gte=window_start)
        .exclude(surge_zone="")
        .values("surge_zone")
        .annotate(riders=Count("rider", distinct=True))
        .values_list("surge_zone", "riders")
    )
    # Latest fresh multiplier per zone (DISTINCT ON zone, newest first).
    stale_after = now - timedelta(seconds=policy.stale_after_seconds)
    latest = {
        z.zone: z.multiplier
        for z in SurgeZone.objects.filter(computed_at__gte=stale_after)
        .order_by("zone", "-computed_at")
        .distinct("zone")
    }
    # Zones with demand, plus surging zones that must decay back towards 1.0.
    zones = set(demand) | {zone for zone, value in latest.items() if value > ONE}
    written = []
    for zone in sorted(zones):
        if not _surge_allowed(zone, policy):
            target, supply = ONE, 0
        else:
            supply = free_supply(zone, now=now)
            target = policy.target(demand.get(zone, 0), supply)
        value = policy.smooth(latest.get(zone, ONE), target)
        written.append(
            SurgeZone(
                zone=zone,
                multiplier=value,
                demand=demand.get(zone, 0),
                supply=supply,
                rule_set_version=rule_set.version,
                computed_at=now,
            )  # fmt: skip
        )
    SurgeZone.objects.bulk_create(written)
    for z in written:
        metrics().gauge("pricing.surge.multiplier", float(z.multiplier), tags={"zone": z.zone})
    logger.info("pricing.surge.updated", extra={"zones": len(written),
                "surging": sum(z.multiplier > ONE for z in written)})  # fmt: skip
    return written


def prune_history(*, older_than_days: int = 7) -> int:
    return SurgeZone.objects.filter(
        computed_at__lt=timezone.now() - timedelta(days=older_than_days)
    ).delete()[0]
