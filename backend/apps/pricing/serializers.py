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
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    currency = serializers.CharField()
    distance_km = serializers.DecimalField(max_digits=10, decimal_places=3)
    duration_minutes = serializers.IntegerField()
    category = serializers.CharField()
