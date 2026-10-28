from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.permissions import IsRider

from .serializers import FareEstimateRequestSerializer, FareEstimateSerializer
from .services import Point, get_fare_estimator


class FareEstimateView(APIView):
    permission_classes = [IsRider]

    @extend_schema(request=FareEstimateRequestSerializer, responses=FareEstimateSerializer)
    def post(self, request):
        serializer = FareEstimateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        estimate = get_fare_estimator().estimate(
            pickup=Point(data["pickup"]["lat"], data["pickup"]["lng"]),
            dropoff=Point(data["dropoff"]["lat"], data["dropoff"]["lng"]),
            category=data["ride_category"],
        )
        return Response(FareEstimateSerializer(estimate).data)
