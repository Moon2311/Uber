"""Unit tests for the separate components: ETA, ranking, discovery, driver state."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from apps.matching.discovery import KRingSearch, RawCandidate
from apps.matching.eta import (
    FALLBACK_SOURCE,
    EtaResult,
    FallbackEtaProvider,
    OsrmEtaProvider,
    ResilientEtaService,
    get_eta_service,
)
from apps.matching.ranking import WeightedEtaRanking
from apps.matching.state import DriverStateStore, EligibleDriver, NullReservationLocks, Rejection
from apps.trips.models import TripStatus

from .conftest import PICKUP, driver_at, new_trip, north


# --- ETA ---------------------------------------------------------------------------------
def test_fallback_eta_is_labelled_and_monotonic():
    near, far = FallbackEtaProvider().estimate([north(0.5), north(3)], PICKUP)
    assert near.is_fallback and near.source == FALLBACK_SOURCE == "fallback_straight_line"
    assert near.seconds < far.seconds and near.seconds >= 60  # fixed overhead


class _Osrm(BaseHTTPRequestHandler):
    mode = "ok"

    def do_GET(self):  # noqa: N802
        if self.mode == "slow":
            time.sleep(1.0)
        n = self.path.split("/table/v1/driving/")[1].split("?")[0].count(";")
        body = {"code": "Ok", "durations": [[100.0 + 10 * i] for i in range(n)],
                "distances": [[900.0] for _ in range(n)]}  # fmt: skip
        if self.mode == "unroutable":
            body["durations"][0] = [None]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def osrm():
    server = HTTPServer(("127.0.0.1", 0), _Osrm)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    _Osrm.mode = "ok"


def test_osrm_provider_parses_table_response(osrm):
    results = OsrmEtaProvider(osrm, timeout=1).estimate([north(1), north(2)], PICKUP)
    assert [r.seconds for r in results] == [100, 110]
    assert all(not r.is_fallback and r.source == "osrm" for r in results)


@pytest.mark.parametrize("mode", ["slow", "unroutable"])
def test_routing_failures_fall_back_with_label(osrm, mode, recorded_metrics):
    _Osrm.mode = mode
    service = ResilientEtaService(OsrmEtaProvider(osrm, timeout=0.3))
    [result] = service.estimate([north(1)], PICKUP)
    assert result.is_fallback and result.source == FALLBACK_SOURCE
    assert recorded_metrics.count("matching.eta.provider_error", provider="osrm") == 1
    assert recorded_metrics.timings


def test_eta_service_configuration(settings, osrm):
    settings.ETA_PROVIDER = "osrm"
    settings.ETA_OSRM_URL = ""
    with pytest.raises(ValueError):
        get_eta_service()
    settings.ETA_OSRM_URL = osrm
    assert get_eta_service().estimate([north(1)], PICKUP)[0].source == "osrm"


# --- ranking ------------------------------------------------------------------------------
def _eligible(driver_id, rating=5.0, km=1.0):
    from datetime import datetime, timezone
    from decimal import Decimal

    from apps.locations.stores import LocationFix

    fix = LocationFix(driver_id, Decimal("1"), Decimal("1"), datetime.now(timezone.utc))
    return EligibleDriver(RawCandidate(fix, 1, km), str(driver_id), "ECONOMY", rating)


def _eta(seconds, fallback=False):
    return EtaResult(seconds, None, "x", fallback)


def test_ranking_prefers_low_eta_and_penalises_low_rating():
    ranked = WeightedEtaRanking().rank([
        (_eligible(1, rating=5.0), _eta(300)),
        (_eligible(2, rating=4.0), _eta(250)),  # 50 s faster but 1 star = +60 s
        (_eligible(3, rating=5.0), _eta(200)),
    ])  # fmt: skip
    assert [r.driver.driver_id for r in ranked] == [3, 1, 2]
    assert [r.rank for r in ranked] == [1, 2, 3]


def test_ranking_rules_are_configurable():
    cfg = {"rating_weight_seconds": 0.0, "max_pickup_eta_seconds": 280,
           "fallback_eta_penalty_seconds": 100.0}  # fmt: skip
    ranked = WeightedEtaRanking(cfg).rank([
        (_eligible(1), _eta(300)),  # dropped: beyond max ETA
        (_eligible(2, rating=3.0), _eta(250)),
        (_eligible(3), _eta(200, fallback=True)),  # 200 + 100 penalty
    ])  # fmt: skip
    assert [r.driver.driver_id for r in ranked] == [2, 3]


def test_ranking_ties_are_deterministic():
    ranked = WeightedEtaRanking().rank([(_eligible(9, km=1.0), _eta(100)),
                                        (_eligible(4, km=1.0), _eta(100)),
                                        (_eligible(7, km=0.5), _eta(100))])  # fmt: skip
    assert [r.driver.driver_id for r in ranked] == [7, 4, 9]


# --- discovery (Batch + K-Ring over the Redis H3 index) --------------------------------------
pytestmark_db = pytest.mark.django_db


@pytest.mark.django_db
def test_k_ring_expands_until_found(matching_env):
    d = driver_at(*north(2.0))  # several rings away at resolution 8
    rings = list(KRingSearch().rings(*PICKUP, max_k=12, max_radius_km=6))
    found_at = [r.k for r in rings if any(c.driver_id == d.pk for c in r.candidates)]
    assert found_at and found_at[0] >= 2
    hit = next(c for r in rings for c in r.candidates if c.driver_id == d.pk)
    assert hit.straight_line_km == pytest.approx(2.0, rel=0.01)


@pytest.mark.django_db
def test_k_ring_respects_radius_and_ring_caps(matching_env):
    driver_at(*north(5.0))
    assert all(not r.candidates for r in KRingSearch().rings(*PICKUP, max_k=12, max_radius_km=3))
    assert len(list(KRingSearch().rings(*PICKUP, max_k=2, max_radius_km=6))) == 3


# --- driver state store ----------------------------------------------------------------------
@pytest.mark.django_db
def test_state_store_rejects_with_reasons(matching_env):
    from apps.drivers.models import DriverProfile
    from apps.trips.models import Trip

    ok = driver_at(*north(0.2))
    stale = driver_at(*north(0.3), seconds_ago=600)
    offline = driver_at(*north(0.4))
    DriverProfile.objects.filter(pk=offline.pk).update(availability_status="OFFLINE")
    busy = driver_at(*north(0.5))
    Trip.objects.filter(pk=new_trip().pk).update(driver=busy, status=TripStatus.DRIVER_ASSIGNED)
    xl = driver_at(*north(0.6), category="XL")
    no_vehicle = driver_at(*north(0.7))
    no_vehicle.vehicles.update(is_active=False)
    excluded = driver_at(*north(0.8))

    raws = [
        RawCandidate(_fix_for(d, s), 1, 0.5)
        for d, s in [(ok, 0), (stale, 600), (offline, 0), (busy, 0), (xl, 0), (no_vehicle, 0),
                     (excluded, 0)]
    ]  # fmt: skip
    eligible, rejected = DriverStateStore(locks=NullReservationLocks()).filter_eligible(
        raws, ride_category="ECONOMY", trip_id="t", exclude={excluded.pk}
    )
    assert [e.driver_id for e in eligible] == [ok.pk]
    assert dict(rejected) == {
        Rejection.STALE.value: 1, Rejection.OFFLINE.value: 1, Rejection.BUSY.value: 1,
        Rejection.INCOMPATIBLE.value: 1, Rejection.INELIGIBLE.value: 1,
        Rejection.EXCLUDED.value: 1,
    }  # fmt: skip


def _fix_for(driver, seconds_ago):
    from datetime import timedelta
    from decimal import Decimal

    from django.utils import timezone

    from apps.locations.stores import LocationFix

    return LocationFix(driver.pk, Decimal("37.78"), Decimal("-122.42"),
                       timezone.now() - timedelta(seconds=seconds_ago))  # fmt: skip


@pytest.mark.django_db
def test_min_rating_rule(matching_env, settings):
    settings.MATCHING_MIN_DRIVER_RATING = 4.5
    low = driver_at(*north(0.2), rating="4.20")
    _, rejected = DriverStateStore(locks=NullReservationLocks()).filter_eligible(
        [RawCandidate(_fix_for(low, 0), 0, 0.2)], ride_category="ECONOMY", trip_id="t",
        exclude=set(),
    )  # fmt: skip
    assert rejected == {"ineligible": 1}


@pytest.mark.django_db
def test_category_compatibility_is_configurable(matching_env, settings):
    comfort = driver_at(*north(0.2), category="COMFORT")
    store = DriverStateStore(locks=NullReservationLocks())
    raw = [RawCandidate(_fix_for(comfort, 0), 0, 0.2)]
    assert store.filter_eligible(raw, ride_category="ECONOMY", trip_id="t", exclude=set())[0] == []
    settings.MATCHING_CATEGORY_COMPATIBILITY = {**settings.MATCHING_CATEGORY_COMPATIBILITY,
                                                "ECONOMY": ["ECONOMY", "COMFORT"]}  # fmt: skip
    assert len(store.filter_eligible(raw, ride_category="ECONOMY", trip_id="t",
                                     exclude=set())[0]) == 1  # fmt: skip
