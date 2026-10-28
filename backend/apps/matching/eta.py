"""Pickup ETA estimation ("Deep ETA" component of the DISCO-style design).

This is a provider abstraction, not a machine-learned ETA model:
  * OsrmEtaProvider    - road-network driving time from an OSRM-compatible routing
                         service (the /table API), when ETA_PROVIDER=osrm.
  * FallbackEtaProvider - NOT a routing result: straight-line distance x road
                         factor / assumed city speed + fixed overhead. Every result
                         is labelled source="fallback_straight_line" and
                         is_fallback=True so ranking, offers and logs can tell.
ResilientEtaService uses the configured provider and falls back per request when it
times out or fails, recording latency and fallback metrics.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass

from django.conf import settings

from apps.common.geo import haversine_km
from apps.common.metrics import metrics

logger = logging.getLogger("apps.matching")

Point = tuple[float, float]  # (lat, lng)

FALLBACK_SOURCE = "fallback_straight_line"


class EtaUnavailable(Exception):
    pass


@dataclass(frozen=True)
class EtaResult:
    seconds: int
    distance_m: int | None
    source: str
    is_fallback: bool


class EtaProvider(ABC):
    name = "abstract"

    @abstractmethod
    def estimate(self, origins: list[Point], destination: Point) -> list[EtaResult]:
        """Driving time from each origin (driver) to the destination (pickup)."""


class FallbackEtaProvider(EtaProvider):
    name = FALLBACK_SOURCE

    def estimate(self, origins, destination):
        out = []
        for lat, lng in origins:
            km = haversine_km(lat, lng, *destination) * settings.ETA_FALLBACK_ROAD_FACTOR
            seconds = km / settings.ETA_FALLBACK_SPEED_KMH * 3600 + settings.ETA_FALLBACK_OVERHEAD_S
            out.append(EtaResult(int(math.ceil(seconds)), int(km * 1000), self.name, True))
        return out


class OsrmEtaProvider(EtaProvider):
    """OSRM table service: GET /table/v1/driving/{lng,lat;...}?sources=..&destinations=..
    One request for the whole shortlist (many-to-one)."""

    name = "osrm"

    def __init__(self, base_url: str, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def estimate(self, origins, destination):
        coords = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in [*origins, destination])
        n = len(origins)
        url = (
            f"{self.base_url}/table/v1/driving/{coords}"
            f"?sources={';'.join(map(str, range(n)))}&destinations={n}"
            "&annotations=duration,distance"
        )
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:  # noqa: S310
                body = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001 - timeouts, HTTP and parse errors
            raise EtaUnavailable(f"osrm: {exc}") from exc
        if body.get("code") != "Ok":
            raise EtaUnavailable(f"osrm: {body.get('code')}")
        durations = body.get("durations") or []
        distances = body.get("distances") or [[None]] * n
        out = []
        for i in range(n):
            seconds = durations[i][0] if i < len(durations) else None
            if seconds is None:  # unroutable (e.g. other side of a river with no bridge)
                raise EtaUnavailable(f"osrm: no route for origin {i}")
            dist = distances[i][0] if i < len(distances) else None
            out.append(EtaResult(int(math.ceil(seconds)), int(dist) if dist else None, self.name,
                                 False))  # fmt: skip
        return out


class ResilientEtaService:
    def __init__(self, primary: EtaProvider | None, fallback: EtaProvider | None = None):
        self.primary = primary
        self.fallback = fallback or FallbackEtaProvider()

    def estimate(self, origins: list[Point], destination: Point) -> list[EtaResult]:
        if not origins:
            return []
        start = time.perf_counter()
        provider = self.primary or self.fallback
        try:
            results = provider.estimate(origins, destination)
        except EtaUnavailable as exc:
            metrics().increment("matching.eta.provider_error", tags={"provider": provider.name})
            logger.warning("matching.eta.fallback", extra={"provider": provider.name,
                                                           "error": str(exc)[:200]})  # fmt: skip
            provider = self.fallback
            results = provider.estimate(origins, destination)
        elapsed = (time.perf_counter() - start) * 1000
        metrics().timing("matching.eta.latency_ms", elapsed, tags={"provider": provider.name})
        metrics().increment(
            "matching.eta.estimates", len(results), tags={"source": results[0].source}
        )
        return results


def get_eta_service() -> ResilientEtaService:
    if settings.ETA_PROVIDER == "osrm":
        if not settings.ETA_OSRM_URL:
            raise ValueError("ETA_PROVIDER=osrm requires ETA_OSRM_URL")
        return ResilientEtaService(
            OsrmEtaProvider(settings.ETA_OSRM_URL, settings.ETA_TIMEOUT_SECONDS)
        )
    return ResilientEtaService(None)
