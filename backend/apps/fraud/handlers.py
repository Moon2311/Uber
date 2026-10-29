"""`fraud` consumer group: asynchronous monitoring from Kafka events.

Delivery is at-least-once; the EventProcessor's ProcessedEvent ledger plus the
unique (event, signal, subject) key on RiskSignal make redelivery harmless.
Where an event lacks a fact (who cancelled, recent history), the handler reads
PostgreSQL, which is authoritative, instead of trusting event payloads alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from apps.common.geo import haversine_km

from .models import DriverGpsState, Subject
from .scoring import record_signal, refresh_profile, rule


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _signal(subject_type, subject_id, signal, event, at, evidence) -> None:
    if record_signal(
        subject_type=subject_type,
        subject_id=subject_id,
        signal=signal,
        source_event_id=event.event_id,
        observed_at=at,
        evidence=evidence,
    ):
        refresh_profile(subject_type, subject_id)


# --- GPS (LOW reliability: review only, never blocks) -------------------------------------
def on_driver_location(event) -> None:
    p = event.payload
    driver_id, at = p["driver_id"], _ts(p["recorded_at"])
    lat, lng = Decimal(p["lat"]), Decimal(p["lng"])
    state = DriverGpsState.objects.select_for_update().filter(driver_id=driver_id).first()
    if state is None:
        DriverGpsState.objects.create(driver_id=driver_id, latitude=lat, longitude=lng,
                                      recorded_at=at)  # fmt: skip
        return
    if at <= state.recorded_at:
        return  # out-of-order or duplicate sample: no speed can be derived
    cfg = rule("gps_impossible_speed")
    km = haversine_km(float(state.latitude), float(state.longitude), float(lat), float(lng))
    hours = (at - state.recorded_at).total_seconds() / 3600
    state.latitude, state.longitude, state.recorded_at = lat, lng, at
    state.save(update_fields=["latitude", "longitude", "recorded_at"])
    if km >= cfg["min_km"] and km / hours > cfg["max_kmh"]:
        _signal(
            Subject.DRIVER,
            driver_id,
            "gps_impossible_speed",
            event,
            at,
            {"km": round(km, 2), "seconds": round(hours * 3600), "kmh": round(km / hours)},
        )


# --- trips ---------------------------------------------------------------------------------
def on_trip_status_changed(event) -> None:
    from apps.trips.models import Trip

    p = event.payload
    if p["to_status"] != "CANCELLED":
        return
    trip = Trip.objects.filter(pk=p["trip_id"]).only("cancelled_by", "rider_id").first()
    if trip is None or trip.cancelled_by != "RIDER":
        return  # system timeouts are not the rider's doing
    cfg = rule("rider_rapid_cancellations")
    at = _ts(p["changed_at"])
    recent = Trip.objects.filter(
        rider_id=trip.rider_id, cancelled_by="RIDER",
        cancelled_at__gte=at - timedelta(hours=cfg["window_hours"]), cancelled_at__lte=at,
    ).count()  # fmt: skip
    if recent >= cfg["threshold"]:
        _signal(Subject.RIDER, trip.rider_id, "rider_rapid_cancellations", event, at,
                {"cancellations": recent, "window_hours": cfg["window_hours"]})  # fmt: skip


def on_trip_completed(event) -> None:
    from apps.trips.models import Trip

    p = event.payload
    at = _ts(p["completed_at"])
    if p.get("started_at"):
        cfg = rule("implausible_trip_speed")
        km = float(p["estimated_distance_km"])
        hours = max((at - _ts(p["started_at"])).total_seconds(), 1) / 3600
        if km >= cfg["min_km"] and km / hours > cfg["max_avg_kmh"]:
            # Route distance vs elapsed time from server-side milestones (not GPS).
            _signal(Subject.DRIVER, p["driver_id"], "implausible_trip_speed", event, at,
                    {"km": km, "minutes": round(hours * 60, 1)})  # fmt: skip
    cfg = rule("repeated_rider_driver_pair")
    pair = Trip.objects.filter(
        rider_id=p["rider_id"], driver_id=p["driver_id"], status="COMPLETED",
        completed_at__gte=at - timedelta(hours=cfg["window_hours"]), completed_at__lte=at,
    ).count()  # fmt: skip
    if pair >= cfg["threshold"]:
        evidence = {"trips": pair, "rider_id": p["rider_id"], "driver_id": p["driver_id"]}
        _signal(Subject.RIDER, p["rider_id"], "repeated_rider_driver_pair", event, at, evidence)
        _signal(Subject.DRIVER, p["driver_id"], "repeated_rider_driver_pair", event, at, evidence)


# --- payments ------------------------------------------------------------------------------
def on_payment_status_changed(event) -> None:
    from django.conf import settings

    from apps.payments.models import PaymentRecord

    p = event.payload
    if p["to_status"] != "FAILED" or (
        p.get("failure_code") in settings.FRAUD_IGNORED_FAILURE_CODES
    ):
        return
    at = _ts(p["changed_at"])
    _signal(Subject.RIDER, p["rider_id"], "payment_failed", event, at,
            {"failure_code": p.get("failure_code"), "payment_id": p["payment_id"]})  # fmt: skip
    cfg = rule("repeated_payment_failures")
    failures = (
        PaymentRecord.objects.filter(
            rider_id=p["rider_id"], failed_at__gte=at - timedelta(hours=cfg["window_hours"])
        )
        .exclude(failure_code__in=settings.FRAUD_IGNORED_FAILURE_CODES)
        .count()
    )
    if failures >= cfg["threshold"]:
        _signal(Subject.RIDER, p["rider_id"], "repeated_payment_failures", event, at,
                {"failures": failures, "window_hours": cfg["window_hours"]})  # fmt: skip


HANDLERS = {
    "DriverLocationUpdated": on_driver_location,
    "TripStatusChanged": on_trip_status_changed,
    "TripCompleted": on_trip_completed,
    "PaymentStatusChanged": on_payment_status_changed,
}
