"""Risk signals -> risk score -> decision. All thresholds live in settings
(FRAUD_RULES, FRAUD_POLICY) so they can be tuned without code changes.

Two kinds of use, deliberately separate:
  * MONITORING (asynchronous, Kafka consumer): record signals, recompute the
    profile, open a FraudCase for a human. Never blocks anyone by itself.
  * BLOCKING CHECK (synchronous, guards.check on sensitive actions): reads the
    current profile and decides ALLOW / REVIEW / BLOCK.

A BLOCK needs CORROBORATION: the score from MEDIUM/HIGH-reliability signals alone
must reach `block_score` AND come from at least `min_distinct_reliable_signals`
different signal types. GPS-derived signals are LOW reliability (spoofable, noisy,
tunnels, multipath), count only towards review, and are capped per signal type,
so no amount of GPS anomalies can block a user automatically. A reviewer's manual
hold is the only other path to BLOCK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.common.metrics import metrics

from .models import (
    CaseStatus,
    Decision,
    FraudCase,
    Reliability,
    RiskLevel,
    RiskProfile,
    RiskSignal,
)


def rule(name: str) -> dict:
    return settings.FRAUD_RULES[name]


def policy() -> dict:
    return settings.FRAUD_POLICY


def record_signal(*, subject_type, subject_id, signal, source_event_id, observed_at,
                  evidence=None) -> RiskSignal | None:  # fmt: skip
    """Idempotent per (event, signal, subject). Returns None for a replay."""
    cfg = rule(signal)
    try:
        with transaction.atomic():
            row = RiskSignal.objects.create(
                subject_type=subject_type, subject_id=subject_id, signal=signal,
                reliability=cfg["reliability"], weight=float(cfg["weight"]),
                evidence=evidence or {}, source_event_id=source_event_id, observed_at=observed_at,
                expires_at=observed_at + timedelta(hours=cfg["ttl_hours"]),
            )  # fmt: skip
    except IntegrityError:
        return None
    metrics().increment("fraud.signal", tags={"signal": signal, "subject": subject_type})
    return row


@dataclass
class Score:
    score: float = 0.0
    blocking_score: float = 0.0
    points: dict[str, float] = field(default_factory=dict)
    reliable_types: set[str] = field(default_factory=set)

    @property
    def level(self) -> str:
        if self.score >= policy()["block_score"]:
            return RiskLevel.HIGH
        if self.score >= policy()["review_score"]:
            return RiskLevel.ELEVATED
        return RiskLevel.LOW


def compute(subject_type, subject_id, *, now=None) -> Score:
    now = now or timezone.now()
    cap = float(policy()["max_points_per_signal"])
    totals: dict[str, tuple[float, str]] = {}
    for signal, weight, reliability in RiskSignal.objects.filter(
        subject_type=subject_type, subject_id=subject_id, expires_at__gt=now
    ).values_list("signal", "weight", "reliability"):
        points, _ = totals.get(signal, (0.0, reliability))
        totals[signal] = (points + weight, reliability)
    result = Score()
    for signal, (points, reliability) in totals.items():
        capped = min(points, cap)  # one noisy signal type cannot dominate
        result.points[signal] = capped
        result.score += capped
        if reliability != Reliability.LOW:
            result.blocking_score += capped
            result.reliable_types.add(signal)
    result.score = min(100.0, result.score)
    result.blocking_score = min(100.0, result.blocking_score)
    return result


def refresh_profile(subject_type, subject_id, *, now=None) -> RiskProfile:
    """Recompute after new signals; open a review case when the level rises."""
    score = compute(subject_type, subject_id, now=now)
    profile, _ = RiskProfile.objects.select_for_update().get_or_create(
        subject_type=subject_type, subject_id=subject_id
    )
    profile.score, profile.blocking_score = score.score, score.blocking_score
    profile.level, profile.signals = score.level, score.points
    profile.save(update_fields=["score", "blocking_score", "level", "signals", "updated_at"])
    if score.level != RiskLevel.LOW:
        _, created = FraudCase.objects.get_or_create(
            subject_type=subject_type, subject_id=subject_id, status=CaseStatus.OPEN,
            defaults={"reason": ", ".join(sorted(score.points)), "score_at_open": score.score},
        )  # fmt: skip
        if created:
            metrics().increment("fraud.case.opened", tags={"level": score.level})
    return profile


@dataclass(frozen=True)
class Assessment:
    decision: str
    score: float
    blocking_score: float
    reasons: list[str]


def decide(subject_type, subject_id, *, now=None) -> Assessment:
    profile = RiskProfile.objects.filter(subject_type=subject_type, subject_id=subject_id).first()
    if profile is not None and profile.hold:
        return Assessment(Decision.BLOCK, profile.score, profile.blocking_score, ["manual_hold"])
    score = compute(subject_type, subject_id, now=now)  # fresh: expiries since last refresh
    reasons = sorted(score.points)
    p = policy()
    corroborated = len(score.reliable_types) >= p["min_distinct_reliable_signals"]
    if score.blocking_score >= p["block_score"] and corroborated:
        return Assessment(Decision.BLOCK, score.score, score.blocking_score, reasons)
    if score.score >= p["review_score"]:
        return Assessment(Decision.REVIEW, score.score, score.blocking_score, reasons)
    return Assessment(Decision.ALLOW, score.score, score.blocking_score, reasons)
