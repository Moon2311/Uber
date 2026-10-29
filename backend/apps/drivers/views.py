from django.conf import settings
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.permissions import IsDriver, IsRider

from .serializers import (
    AvailabilitySerializer,
    DriverProfileSerializer,
    NearbyDriverSerializer,
    NearbyQuerySerializer,
    NearbyResponseSerializer,
)
from .services import find_nearby_drivers, set_availability


class AvailabilityView(APIView):
    permission_classes = [IsDriver]

    @extend_schema(responses=DriverProfileSerializer)
    def get(self, request):
        return Response(DriverProfileSerializer(request.user.driver_profile).data)

    @extend_schema(request=AvailabilitySerializer, responses=DriverProfileSerializer)
    def post(self, request):
        serializer = AvailabilitySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        driver = set_availability(
            driver=request.user.driver_profile, online=serializer.validated_data["is_online"]
        )
        return Response(DriverProfileSerializer(driver).data)


class NearbyDriversView(APIView):
    permission_classes = [IsRider]

    @extend_schema(
        parameters=[
            OpenApiParameter("lat", float, required=True),
            OpenApiParameter("lng", float, required=True),
            OpenApiParameter("radius_km", float, required=False),
            OpenApiParameter("category", str, required=False, enum=["ECONOMY", "COMFORT", "XL"]),
        ],
        responses=NearbyResponseSerializer,
    )
    def get(self, request):
        query = NearbyQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        drivers = find_nearby_drivers(
            lat=float(params["lat"]),
            lng=float(params["lng"]),
            radius_km=params["radius_km"],
            category=params.get("category"),
            limit=settings.NEARBY_DRIVERS_MAX_RESULTS,
        )
        return Response(
            {
                "count": len(drivers),
                "radius_km": params["radius_km"],
                "results": NearbyDriverSerializer(
                    drivers, many=True, context={"viewer_id": request.user.pk}
                ).data,
            }
        )
