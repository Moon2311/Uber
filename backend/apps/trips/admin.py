from django.contrib import admin

from .models import Trip


@admin.register(Trip)
class TripAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "rider",
        "driver",
        "status",
        "ride_category",
        "estimated_fare",
        "created_at",
    )
    list_filter = ("status", "ride_category")
    search_fields = ("id", "rider__email", "driver__user__email")
    readonly_fields = ("id", "created_at", "updated_at")
