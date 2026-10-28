from django.contrib import admin

from .models import Trip, TripEvent


@admin.register(Trip)
class TripAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "rider",
        "driver",
        "status",
        "state_version",
        "ride_category",
        "estimated_fare",
        "created_at",
    )
    list_filter = ("status", "ride_category")
    search_fields = ("id", "rider__email", "driver__user__email")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(TripEvent)
class TripEventAdmin(admin.ModelAdmin):
    """Append-only: viewable, never editable."""

    list_display = ("id", "trip", "event_type", "from_status", "to_status", "actor_type",
                    "occurred_at")  # fmt: skip
    list_filter = ("event_type", "actor_type")
    search_fields = ("trip__id", "event_id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
