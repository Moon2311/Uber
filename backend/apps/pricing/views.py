from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.permissions import IsRider

from .serializers import FareEstimateRequestSerializer, FareEstimateSerializer
from .services import quote_fare


class FareEstimateView(APIView):
    """Price a ride. Every estimate is a short-lived, recorded quote."""

    permission_classes = [IsRider]
    throttle_scope = "fare_estimate"

    @extend_schema(request=FareEstimateRequestSerializer, responses=FareEstimateSerializer)
    def post(self, request):
        serializer = FareEstimateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        quote = quote_fare(
            rider=request.user, pickup=data["pickup"], dropoff=data["dropoff"],
            ride_category=data["ride_category"],
        )  # fmt: skip
        return Response(FareEstimateSerializer(quote).data)
