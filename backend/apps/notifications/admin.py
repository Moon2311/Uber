from django.contrib import admin

from .models import DeviceToken, Notification, NotificationDelivery


class DeliveryInline(admin.TabularInline):
    model = NotificationDelivery
    extra = 0
    can_delete = False
    readonly_fields = ("device", "status", "provider_message_id", "error", "attempts", "updated_at")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "kind", "status", "attempts", "created_at", "sent_at")
    list_filter = ("status", "kind")
    inlines = [DeliveryInline]
    readonly_fields = [f.name for f in Notification._meta.fields]


@admin.register(DeviceToken)
class DeviceTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "platform", "is_active", "last_seen_at")
    list_filter = ("platform", "is_active")
