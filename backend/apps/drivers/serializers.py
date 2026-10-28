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


class NearbyDriverSerializer(serializers.Serializer):
    """Deliberately minimal: no names, plates or contact details."""

    id = serializers.CharField(source="public_id")
    lat = serializers.FloatField(source="latitude")
    lng = serializers.FloatField(source="longitude")
    heading = serializers.FloatField(allow_null=True)
    distance_km = serializers.FloatField()
    category = serializers.CharField()


class NearbyResponseSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    radius_km = serializers.FloatField()
    results = NearbyDriverSerializer(many=True)
