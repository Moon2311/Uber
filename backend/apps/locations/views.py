from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.permissions import IsDriver

from .serializers import (
    LocationUpdateResponseSerializer,
    LocationUpdateSerializer,
    MyLocationResponseSerializer,
    location_payload,
)
from .service import get_location_service


class LocationUpdateView(APIView):
    """Accept a GPS fix from the authenticated driver.

    200 + `accepted: true`  -> stored as the driver's latest location.
    200 + `accepted: false` -> valid but not applied (`outcome` explains why:
                               out_of_order, too_old, low_accuracy). Clients must not retry.
    400 -> invalid payload; 409 `driver_offline` -> driver must go online first.
    """

    permission_classes = [IsDriver]
    throttle_scope = "location_update"

    @extend_schema(request=LocationUpdateSerializer, responses=LocationUpdateResponseSerializer)
    def post(self, request):
        serializer = LocationUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        service = get_location_service()
        result = service.record_driver_update(
            driver=request.user.driver_profile,
            latitude=data["lat"],
            longitude=data["lng"],
            recorded_at=data.get("recorded_at"),
            heading=data.get("heading"),
            speed_kmh=data.get("speed_kmh"),
            accuracy_m=data.get("accuracy_m"),
        )
        return Response(
            {
                "accepted": result.accepted,
                "outcome": result.outcome.value,
                "freshness": service.freshness(result.location).value,
                **location_payload(result.location),
            }
        )


class MyLocationView(APIView):
    """The authenticated driver's latest stored location and its freshness."""

    permission_classes = [IsDriver]

    @extend_schema(responses=MyLocationResponseSerializer)
    def get(self, request):
        service = get_location_service()
        fix = service.get_latest(request.user.driver_profile.pk)
        return Response(
            {
                "freshness": service.freshness(fix).value,
                "stale_after_seconds": settings.DRIVER_LOCATION_STALE_SECONDS,
                "location": location_payload(fix) if fix else None,
            }
        )
