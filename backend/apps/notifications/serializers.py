from rest_framework import serializers

from .models import DeviceToken, Notification


class DeviceTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceToken
        fields = ["id", "platform", "token", "is_active", "created_at"]
        read_only_fields = ["id", "is_active", "created_at"]
        extra_kwargs = {"token": {"write_only": True, "validators": []}}


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "kind", "title", "body", "data", "status", "created_at", "sent_at"]
        read_only_fields = fields
