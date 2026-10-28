"""Domain object -> event. The only place that knows both shapes."""

from .schemas import Event, make_event


def driver_availability_changed(
    *, driver_id, status, previous_status, reason, vehicle_category, changed_at
) -> Event:
    return make_event(
        "DriverAvailabilityChanged",
        {
            "driver_id": driver_id,
            "status": status,
            "previous_status": previous_status,
            "reason": reason,
            "vehicle_category": vehicle_category,
            "changed_at": changed_at,
        },
    )


def ride_requested(trip) -> Event:
    from apps.geoindex.h3_index import GeoIndexError
    from apps.geoindex.service import get_geo_search_service

    try:
        cell = get_geo_search_service().cell_for(trip.pickup_latitude, trip.pickup_longitude)
    except GeoIndexError:
        cell = None
    return make_event(
        "RideRequested",
        {
            "trip_id": trip.id,
            "rider_id": trip.rider_id,
            "ride_category": trip.ride_category,
            "pickup_lat": trip.pickup_latitude,
            "pickup_lng": trip.pickup_longitude,
            "dropoff_lat": trip.dropoff_latitude,
            "dropoff_lng": trip.dropoff_longitude,
            "pickup_h3_cell": cell,
            "estimated_fare": trip.estimated_fare,
            "currency": trip.currency,
            "requested_at": trip.created_at,
        },
    )


def trip_status_changed(trip, from_status, to_status, reason=None) -> Event:
    return make_event(
        "TripStatusChanged",
        {
            "trip_id": trip.id,
            "from_status": from_status,
            "to_status": to_status,
            "rider_id": trip.rider_id,
            "driver_id": trip.driver_id,
            "reason": reason or None,
            "changed_at": trip.updated_at,
        },
    )


def trip_completed(trip) -> Event:
    return make_event(
        "TripCompleted",
        {
            "trip_id": trip.id,
            "rider_id": trip.rider_id,
            "driver_id": trip.driver_id,
            "ride_category": trip.ride_category,
            "estimated_fare": trip.estimated_fare,
            "final_fare": None,  # fare finalisation arrives with payments
            "currency": trip.currency,
            "estimated_distance_km": trip.estimated_distance_km,
            "started_at": trip.started_at,
            "completed_at": trip.completed_at,
        },
    )


def ride_offer_created(
    *, trip_id, offer_id, driver_id, expires_at, created_at, straight_line_km=None,
    grid_distance=None
) -> Event:  # fmt: skip
    """Emitted by the matching engine (later step)."""
    return make_event(
        "RideOfferCreated",
        {
            "trip_id": trip_id,
            "offer_id": offer_id,
            "driver_id": driver_id,
            "expires_at": expires_at,
            "straight_line_km": straight_line_km,
            "grid_distance": grid_distance,
            "created_at": created_at,
        },
    )


def ride_accepted(*, trip_id, offer_id, driver_id, accepted_at) -> Event:
    """Emitted by the offer-acceptance flow (later step)."""
    return make_event(
        "RideAccepted",
        {
            "trip_id": trip_id,
            "offer_id": offer_id,
            "driver_id": driver_id,
            "accepted_at": accepted_at,
        },
    )


def driver_location_updated(fix, reason: str) -> Event:
    return make_event(
        "DriverLocationUpdated",
        {
            "driver_id": fix.driver_id,
            "lat": fix.latitude,
            "lng": fix.longitude,
            "recorded_at": fix.recorded_at,
            "h3_cell": fix.h3_cell,
            "h3_resolution": fix.h3_resolution,
            "heading": fix.heading,
            "speed_kmh": fix.speed_kmh,
            "reason": reason,
        },
    )
