"""Authorization + authoritative snapshots (synchronous, PostgreSQL)."""

from __future__ import annotations

import json

from rest_framework.renderers import JSONRenderer

from apps.matching.models import OfferStatus, RideOffer
from apps.matching.serializers import RideOfferSerializer
from apps.trips.models import ACTIVE_STATUSES, Trip
from apps.trips.serializers import TripSerializer


def trip_for_user(user, trip_id) -> Trip | None:
    """The trip if `user` may watch it: its rider, or its *assigned* driver.
    Drivers who were merely offered a trip get the offer payload, never the trip
    channel (that would leak rider details before acceptance)."""
    qs = Trip.objects.select_related("rider", "driver__user").filter(pk=trip_id)
    if user.is_staff:
        return qs.first()
    if user.is_driver and hasattr(user, "driver_profile"):
        return qs.filter(driver=user.driver_profile).first()
    return qs.filter(rider=user).first()


def active_trip_ids(user) -> list[str]:
    qs = Trip.objects.filter(status__in=ACTIVE_STATUSES)
    if user.is_driver and hasattr(user, "driver_profile"):
        qs = qs.filter(driver=user.driver_profile)
    else:
        qs = qs.filter(rider=user)
    return [str(pk) for pk in qs.values_list("pk", flat=True)]


def plain(data) -> dict:
    """JSON-native copy (Decimal/UUID/datetime rendered exactly as the REST API does),
    safe for both the WebSocket encoder and the Redis channel layer (msgpack)."""
    return json.loads(JSONRenderer().render(data))


def trip_snapshot(trip: Trip) -> dict:
    data = plain(TripSerializer(trip).data)
    data["version"] = trip.updated_at.isoformat()  # clients drop older versions
    return data


def offer_payload(offer: RideOffer) -> dict:
    return plain(RideOfferSerializer(offer).data)


def pending_offers(user) -> list[dict]:
    if not (user.is_driver and hasattr(user, "driver_profile")):
        return []
    from django.utils import timezone

    offers = RideOffer.objects.filter(
        driver=user.driver_profile, status=OfferStatus.PENDING, expires_at__gt=timezone.now()
    ).select_related("trip")
    return [offer_payload(o) for o in offers]
