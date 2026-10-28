from rest_framework import serializers

from apps.common.serializers import LatitudeField, LongitudeField
from apps.drivers.models import RideCategory


class LocationPointSerializer(serializers.Serializer):
    lat = LatitudeField()
    lng = LongitudeField()
    address = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class FareEstimateRequestSerializer(serializers.Serializer):
    pickup = LocationPointSerializer()
    dropoff = LocationPointSerializer()
    ride_category = serializers.ChoiceField(choices=RideCategory.choices)

    def validate(self, attrs):
        p, d = attrs["pickup"], attrs["dropoff"]
        if p["lat"] == d["lat"] and p["lng"] == d["lng"]:
            raise serializers.ValidationError(
                {"dropoff": ["Destination must be different from pickup."]}
            )
        return attrs


class FareEstimateSerializer(serializers.Serializer):
    """An upfront quote. Pass `quote_id` to POST /trips/ to ride at this price."""

    quote_id = serializers.UUIDField(source="id")
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    currency = serializers.CharField()
    distance_km = serializers.DecimalField(max_digits=10, decimal_places=3)
    duration_minutes = serializers.IntegerField()
    category = serializers.CharField(source="ride_category")
    surge_multiplier = serializers.DecimalField(max_digits=4, decimal_places=2)
    pricing_version = serializers.IntegerField(source="rule_set.version")
    breakdown = serializers.JSONField()
    expires_at = serializers.DateTimeField()
