from django.db import transaction
from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import DeviceToken, Notification
from .serializers import DeviceTokenSerializer, NotificationSerializer


class DeviceListView(APIView):
    """Register this device for push. Re-registering a token moves it to the caller
    (the device changed hands / user switched accounts)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(request=DeviceTokenSerializer, responses={201: DeviceTokenSerializer})
    def post(self, request):
        serializer = DeviceTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            device, _ = DeviceToken.objects.update_or_create(
                token=serializer.validated_data["token"],
                defaults={"user": request.user, "platform": serializer.validated_data["platform"],
                          "is_active": True},
            )  # fmt: skip
        return Response(DeviceTokenSerializer(device).data, status=status.HTTP_201_CREATED)


class DeviceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={204: None})
    def delete(self, request, device_id):
        if not DeviceToken.objects.filter(pk=device_id, user=request.user).update(is_active=False):
            raise Http404
        return Response(status=status.HTTP_204_NO_CONTENT)


class NotificationListView(generics.ListAPIView):
    """The caller's recent notifications with delivery status (an in-app inbox)."""

    permission_classes = [IsAuthenticated]
    serializer_class = NotificationSerializer

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user).order_by("-created_at")
