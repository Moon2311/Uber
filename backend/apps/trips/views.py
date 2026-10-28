from django.http import Http404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.idempotency import idempotent_response
from apps.common.permissions import IsDriver, IsRider

from .models import Trip, TripStatus
from .serializers import TripCancelSerializer, TripCreateSerializer, TripSerializer
from .services import advance_trip_by_driver, cancel_trip_by_rider, request_trip


def _trips_for(user):
    return Trip.objects.visible_to(user).select_related("rider", "driver__user")


class TripCreateView(APIView):
    permission_classes = [IsRider]
    throttle_scope = "trip_create"

    @extend_schema(
        request=TripCreateSerializer,
        responses={201: TripSerializer},
        parameters=[
            OpenApiParameter(
                "Idempotency-Key", str, location=OpenApiParameter.HEADER, required=False,
                description="Retry-safe key: repeating the request returns the same trip.",
            )
        ],
    )  # fmt: skip
    def post(self, request):
        def run():
            serializer = TripCreateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            trip = request_trip(rider=request.user, **serializer.validated_data)
            return Response(TripSerializer(trip).data, status=status.HTTP_201_CREATED)

        return idempotent_response(request, "trip-create", run)


class TripDetailView(generics.RetrieveAPIView):
    """404 (not 403) for trips the caller does not own, to avoid leaking existence."""

    permission_classes = [IsAuthenticated]
    serializer_class = TripSerializer
    lookup_url_kwarg = "trip_id"

    def get_queryset(self):
        return _trips_for(self.request.user)


class TripHistoryView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TripSerializer

    @extend_schema(
        parameters=[OpenApiParameter("status", str, enum=TripStatus.values, required=False)]
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        qs = _trips_for(self.request.user).order_by("-created_at")
        status_filter = self.request.query_params.get("status")
        if status_filter in TripStatus.values:
            qs = qs.filter(status=status_filter)
        return qs


class TripCancelView(APIView):
    permission_classes = [IsRider]

    @extend_schema(request=TripCancelSerializer, responses=TripSerializer)
    def post(self, request, trip_id):
        serializer = TripCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            trip = cancel_trip_by_rider(
                trip_id=trip_id, rider=request.user, reason=serializer.validated_data["reason"]
            )
        except Trip.DoesNotExist as exc:
            raise Http404 from exc
        return Response(TripSerializer(trip).data)


class TripMilestoneView(APIView):
    """POST /trips/{id}/arrive|start|complete/ - assigned driver only."""

    permission_classes = [IsDriver]
    milestone = None

    @extend_schema(request=None, responses=TripSerializer)
    def post(self, request, trip_id):
        try:
            trip = advance_trip_by_driver(
                trip_id=trip_id, driver=request.user.driver_profile, milestone=self.milestone
            )
        except Trip.DoesNotExist as exc:
            raise Http404 from exc
        return Response(TripSerializer(trip).data)
