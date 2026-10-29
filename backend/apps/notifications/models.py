"""Push notifications with durable delivery tracking (PostgreSQL).

Push is for IMPORTANT alerts that must reach a phone in a pocket (driver assigned,
driver arrived, new ride offer, payment failed, receipt). Live in-app state
(status changes, driver position) travels over WebSockets (apps.realtime) and is
never duplicated here.
"""

import uuid

from django.conf import settings
from django.db import models


class Platform(models.TextChoices):
    IOS = "IOS", "iOS"
    ANDROID = "ANDROID", "Android"
    WEB = "WEB", "Web push"


class DeviceToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="devices"
    )
    platform = models.CharField(max_length=10, choices=Platform.choices)
    token = models.CharField(max_length=512, unique=True)  # push-service token (not a secret)
    is_active = models.BooleanField(default=True)  # False once the provider says it is invalid
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.platform} device of {self.user_id}"


class NotificationStatus(models.TextChoices):
    PENDING = "PENDING", "Pending (queued or retrying)"
    SENT = "SENT", "Sent to at least one device"
    FAILED = "FAILED", "Failed after retries"
    SKIPPED = "SKIPPED", "Skipped (no device / expired)"


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    kind = models.CharField(max_length=40)
    title = models.CharField(max_length=120)
    body = models.CharField(max_length=255)
    data = models.JSONField(default=dict)  # ids only (trip_id, payment_id): no PII
    # source event + kind + recipient: a redelivered event never notifies twice.
    dedupe_key = models.CharField(max_length=150, unique=True)
    collapse_key = models.CharField(max_length=100, blank=True)  # newer replaces older on device
    status = models.CharField(
        max_length=10, choices=NotificationStatus.choices, default=NotificationStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    expires_at = models.DateTimeField()  # an "arrived" alert an hour late is noise
    last_error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["next_attempt_at"],
                name="notification_due_idx",
                condition=models.Q(status="PENDING"),
            ),  # fmt: skip
            models.Index(fields=["user", "-created_at"], name="notification_user_idx"),
        ]

    def __str__(self):
        return f"{self.kind} -> {self.user_id} [{self.status}]"


class DeliveryStatus(models.TextChoices):
    SENT = "SENT", "Accepted by the push service"
    RETRYING = "RETRYING", "Transient failure"
    FAILED = "FAILED", "Permanent failure"
    INVALID_TOKEN = "INVALID_TOKEN", "Token rejected; device deactivated"


class NotificationDelivery(models.Model):
    """Per-device outcome. A device that already got a notification is never sent
    it again when the notification is retried for another device."""

    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="deliveries"
    )
    device = models.ForeignKey(DeviceToken, on_delete=models.CASCADE, related_name="deliveries")
    status = models.CharField(max_length=14, choices=DeliveryStatus.choices)
    provider_message_id = models.CharField(max_length=255, blank=True)
    error = models.CharField(max_length=255, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["notification", "device"], name="delivery_once")
        ]

    def __str__(self):
        return f"{self.notification_id} -> {self.device_id} [{self.status}]"
