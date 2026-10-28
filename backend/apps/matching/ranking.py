"""Ranking: turn eligible drivers + pickup ETAs into an ordered offer list.

Configurable via settings.MATCHING_RANKING (lower score = better):
    score = eta_weight * pickup_eta_seconds
          + rating_weight_seconds * (5 - rating)          # each missing star "costs" N s
          + fallback_eta_penalty_seconds (if the ETA is a fallback estimate)
Drivers whose ETA exceeds max_pickup_eta_seconds are dropped.
Ties are broken by straight-line distance, then driver id (deterministic).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from django.conf import settings

from .eta import EtaResult
from .state import EligibleDriver


@dataclass(frozen=True)
class RankedCandidate:
    driver: EligibleDriver
    eta: EtaResult
    score: float
    rank: int


class RankingPolicy(ABC):
    @abstractmethod
    def rank(self, scored: list[tuple[EligibleDriver, EtaResult]]) -> list[RankedCandidate]: ...


class WeightedEtaRanking(RankingPolicy):
    def __init__(self, config: dict | None = None):
        self.cfg = {**settings.MATCHING_RANKING, **(config or {})}

    def score(self, driver: EligibleDriver, eta: EtaResult) -> float:
        c = self.cfg
        score = c["eta_weight"] * eta.seconds
        score += c["rating_weight_seconds"] * max(0.0, 5.0 - driver.rating)
        if eta.is_fallback:
            score += c["fallback_eta_penalty_seconds"]
        return score

    def rank(self, scored):
        kept = [
            (d, e, self.score(d, e))
            for d, e in scored
            if e.seconds <= self.cfg["max_pickup_eta_seconds"]
        ]
        kept.sort(key=lambda t: (t[2], t[0].raw.straight_line_km, t[0].driver_id))
        return [RankedCandidate(d, e, s, i) for i, (d, e, s) in enumerate(kept, start=1)]
