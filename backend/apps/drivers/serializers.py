from datetime import datetime

from django.conf import settings
from rest_framework import serializers

from apps.common.serializers import LatitudeField, LongitudeField

from .models import DriverProfile, RideCategory, Vehicle
from .services import get_driver_availability


class VehicleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vehicle
        fields = ["make", "model", "color", "plate_number", "year", "category"]
        read_only_fields = fields


class DriverProfileSerializer(serializers.ModelSerializer):
    is_online = serializers.BooleanField(read_only=True)
    vehicle = serializers.SerializerMethodField()
    # Step 2: availability = ONLINE *and* a fresh location.
    is_available = serializers.SerializerMethodField()
    location_freshness = serializers.SerializerMethodField()
    last_location_at = serializers.SerializerMethodField()

    class Meta:
        model = DriverProfile
        fields = [
            "public_id",
            "availability_status",
            "is_online",
            "availability_changed_at",
            "rating",
            "vehicle",
            "is_available",
            "location_freshness",
            "last_location_at",
        ]
        read_only_fields = fields

    def _availability(self, obj):
        cache = self.context.setdefault("_availability", {})
        if obj.pk not in cache:
            cache[obj.pk] = get_driver_availability(obj)
        return cache[obj.pk]

    def get_is_available(self, obj) -> bool:
        return self._availability(obj).is_available

    def get_location_freshness(self, obj) -> str:
        return self._availability(obj).location_freshness.value

    def get_last_location_at(self, obj) -> datetime | None:
        return self._availability(obj).last_location_at

    def get_vehicle(self, obj) -> dict | None:
        vehicle = obj.active_vehicle
        return VehicleSerializer(vehicle).data if vehicle else None


class AvailabilitySerializer(serializers.Serializer):
    is_online = serializers.BooleanField()


class NearbyQuerySerializer(serializers.Serializer):
    lat = LatitudeField()
    lng = LongitudeField()
    radius_km = serializers.FloatField(
        required=False,
        min_value=0.1,
        max_value=settings.NEARBY_DRIVERS_MAX_RADIUS_KM,
        default=settings.NEARBY_DRIVERS_DEFAULT_RADIUS_KM,
    )
    category = serializers.ChoiceField(choices=RideCategory.choices, required=False)


def driver_pseudonym(public_id, viewer_id, *, at=None) -> str:
    """Stable for one rider within one hour (map markers can animate), unlinkable
    across riders and hours: nobody can follow one driver around the city."""
    import hashlib
    import hmac
    import time

    from django.conf import settings

    bucket = int((at or time.time()) // 3600)
    msg = f"{public_id}:{viewer_id}:{bucket}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:20]


class NearbyDriverSerializer(serializers.Serializer):
    """Deliberately minimal: no names, plates or contact details, a rotating
    per-rider pseudonym instead of the driver id, and positions coarsened to
    ~100 m (dispatch uses exact positions server-side; the map does not need them)."""

    id = serializers.SerializerMethodField()
    lat = serializers.SerializerMethodField()
    lng = serializers.SerializerMethodField()
    heading = serializers.FloatField(allow_null=True)
    distance_km = serializers.SerializerMethodField()
    category = serializers.CharField()

    def get_id(self, obj) -> str:
        return driver_pseudonym(obj.public_id, self.context["viewer_id"])

    def get_lat(self, obj) -> float:
        return round(float(obj.latitude), 3)

    def get_lng(self, obj) -> float:
        return round(float(obj.longitude), 3)

    def get_distance_km(self, obj) -> float:
        return round(float(obj.distance_km), 1)


class NearbyResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    radius_km = serializers.FloatField()
    results = NearbyDriverSerializer(many=True)
