from django.contrib import admin

from .models import DriverLocation, DriverLocationEvent


@admin.register(DriverLocation)
class DriverLocationAdmin(admin.ModelAdmin):
    list_display = ("driver", "latitude", "longitude", "recorded_at", "updated_at")
    readonly_fields = ("updated_at",)


@admin.register(DriverLocationEvent)
class DriverLocationEventAdmin(admin.ModelAdmin):
    list_display = ("driver", "latitude", "longitude", "recorded_at", "created_at")
    list_filter = ("recorded_at",)
    search_fields = ("driver__user__email",)
