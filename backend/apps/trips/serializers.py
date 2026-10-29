from rest_framework import serializers

from apps.drivers.models import RideCategory
from apps.pricing.serializers import LocationPointSerializer

from .models import Trip, TripEvent


class TripCreateSerializer(serializers.Serializer):
    pickup = LocationPointSerializer()
    dropoff = LocationPointSerializer()
    ride_category = serializers.ChoiceField(choices=RideCategory.choices)
    quote_id = serializers.UUIDField(required=False, default=None)  # from /pricing/estimate/
    payment_method_id = serializers.UUIDField(required=False, default=None)  # default: card/cash

    def validate(self, attrs):
        # Reject server-controlled fields (driver, rider, status, fare...) outright
        # instead of silently ignoring them, so tampering is visible.
        unknown = set(self.initial_data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {field: ["This field is not allowed."] for field in sorted(unknown)}
            )
        p, d = attrs["pickup"], attrs["dropoff"]
        if p["lat"] == d["lat"] and p["lng"] == d["lng"]:
            raise serializers.ValidationError(
                {"dropoff": ["Destination must be different from pickup."]}
            )
        return attrs


class _VersionedCommand(serializers.Serializer):
    # Optional optimistic-concurrency guard: the `state_version` the client last saw.
    # A mismatch is 409 stale_trip_state instead of acting on an outdated screen.
    expected_version = serializers.IntegerField(min_value=0, required=False, default=None)


class TripCancelSerializer(_VersionedCommand):
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class TripMilestoneSerializer(_VersionedCommand):
    pass


class TripReleaseSerializer(TripCancelSerializer):
    pass


class TripEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = TripEvent
        fields = [
            "event_id", "event_type", "state_version", "from_status", "to_status",
            "actor_type", "actor_id", "driver_id", "offer_id", "reason", "metadata",
            "occurred_at",
        ]  # fmt: skip
        read_only_fields = fields


class _PointOut(serializers.Serializer):
    lat = serializers.DecimalField(max_digits=9, decimal_places=6, coerce_to_string=False)
    lng = serializers.DecimalField(max_digits=9, decimal_places=6, coerce_to_string=False)
    address = serializers.CharField()


class TripSerializer(serializers.ModelSerializer):
    pickup = serializers.SerializerMethodField()
    dropoff = serializers.SerializerMethodField()
    driver = serializers.SerializerMethodField()
    rider = serializers.SerializerMethodField()

    class Meta:
        model = Trip
        fields = [
            "id",
            "status",
            "state_version",
            "ride_category",
            "pickup",
            "dropoff",
            "estimated_fare",
            "fare_quote",
            "pricing_version",
            "surge_multiplier",
            "estimated_distance_km",
            "estimated_duration_minutes",
            "currency",
            "rider",
            "driver",
            "created_at",
            "updated_at",
            "search_started_at",
            "driver_assigned_at",
            "driver_arrived_at",
            "started_at",
            "completed_at",
            "cancelled_at",
            "cancelled_by",
            "cancellation_reason",
        ]
        read_only_fields = fields

    def _point(self, lat, lng, address):
        return _PointOut({"lat": lat, "lng": lng, "address": address}).data

    def get_pickup(self, obj) -> dict:
        return self._point(obj.pickup_latitude, obj.pickup_longitude, obj.pickup_address)

    def get_dropoff(self, obj) -> dict:
        return self._point(obj.dropoff_latitude, obj.dropoff_longitude, obj.dropoff_address)

    def get_rider(self, obj) -> dict:
        # Drivers see only the rider's first name; never email/phone.
        return {"first_name": obj.rider.first_name}

    def get_driver(self, obj) -> dict | None:
        if obj.driver is None:
            return None
        vehicle = obj.driver.active_vehicle
        return {
            "id": str(obj.driver.public_id),
            "first_name": obj.driver.user.first_name,
            "rating": str(obj.driver.rating),
            "vehicle": (
                {
                    "make": vehicle.make,
                    "model": vehicle.model,
                    "color": vehicle.color,
                    "plate_number": vehicle.plate_number,
                }
                if vehicle
                else None
            ),
        }
