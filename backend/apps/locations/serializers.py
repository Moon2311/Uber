from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from apps.common.serializers import LatitudeField, LongitudeField

from .service import Freshness, UpdateOutcome


class AwareDateTimeField(serializers.DateTimeField):
    """Rejects timestamps without an explicit offset instead of assuming server TZ."""

    default_error_messages = {"naive": "Timestamp must include a timezone offset."}

    def enforce_timezone(self, value):
        if timezone.is_naive(value):
            self.fail("naive")
        return super().enforce_timezone(value)


class LocationUpdateSerializer(serializers.Serializer):
    """Driver GPS fix. The driver identity always comes from the JWT, never the body."""

    lat = LatitudeField()
    lng = LongitudeField()
    accuracy_m = serializers.FloatField(
        required=False, allow_null=True, min_value=0, max_value=10000
    )
    heading = serializers.FloatField(
        required=False, allow_null=True, min_value=0, max_value=359.999
    )
    speed_kmh = serializers.FloatField(required=False, allow_null=True, min_value=0, max_value=400)
    recorded_at = AwareDateTimeField(
        required=False, help_text="Device fix time (ISO-8601 with offset). Defaults to now."
    )

    def validate_recorded_at(self, value):
        skew = timedelta(seconds=settings.LOCATION_MAX_FUTURE_SKEW_SECONDS)
        if value > timezone.now() + skew:
            raise serializers.ValidationError("Timestamp is in the future.")
        return value

    def validate(self, attrs):
        unknown = set(self.initial_data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(
                {field: ["This field is not allowed."] for field in sorted(unknown)}
            )
        return attrs


class LocationFixSerializer(serializers.Serializer):
    lat = serializers.DecimalField(
        source="latitude", max_digits=9, decimal_places=6, coerce_to_string=False
    )
    lng = serializers.DecimalField(
        source="longitude", max_digits=9, decimal_places=6, coerce_to_string=False
    )
    heading = serializers.FloatField(allow_null=True)
    speed_kmh = serializers.FloatField(allow_null=True)
    accuracy_m = serializers.FloatField(allow_null=True)
    recorded_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField(allow_null=True)


class LocationUpdateResponseSerializer(serializers.Serializer):
    """Backwards compatible with step 1: location fields stay top-level."""

    accepted = serializers.BooleanField()
    outcome = serializers.ChoiceField(choices=[o.value for o in UpdateOutcome])
    freshness = serializers.ChoiceField(choices=[f.value for f in Freshness])
    lat = serializers.FloatField(allow_null=True)
    lng = serializers.FloatField(allow_null=True)
    heading = serializers.FloatField(allow_null=True)
    speed_kmh = serializers.FloatField(allow_null=True)
    accuracy_m = serializers.FloatField(allow_null=True)
    recorded_at = serializers.DateTimeField(allow_null=True)
    updated_at = serializers.DateTimeField(allow_null=True)


class MyLocationResponseSerializer(serializers.Serializer):
    freshness = serializers.ChoiceField(choices=[f.value for f in Freshness])
    stale_after_seconds = serializers.IntegerField()
    location = LocationFixSerializer(allow_null=True)


_EMPTY_FIX = dict.fromkeys(
    ["lat", "lng", "heading", "speed_kmh", "accuracy_m", "recorded_at", "updated_at"]
)


def location_payload(fix) -> dict:
    return LocationFixSerializer(fix).data if fix else dict(_EMPTY_FIX)
