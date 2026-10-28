from rest_framework import serializers

from apps.trips.serializers import TripSerializer

from .models import RideOffer


class RideOfferSerializer(serializers.ModelSerializer):
    trip = serializers.SerializerMethodField()
    eta_is_estimate = serializers.BooleanField(source="eta_is_fallback", read_only=True)

    class Meta:
        model = RideOffer
        fields = [
            "id",
            "status",
            "trip",
            "expires_at",
            "pickup_eta_seconds",
            "eta_source",
            "eta_is_estimate",
            "straight_line_km",
            "created_at",
            "responded_at",
        ]
        read_only_fields = fields

    def get_trip(self, obj) -> dict:
        data = TripSerializer(obj.trip).data
        # Before acceptance a driver only needs the route and fare, not rider details.
        return {k: data[k] for k in ("id", "ride_category", "pickup", "dropoff",
                                     "estimated_fare", "currency", "status")}  # fmt: skip
