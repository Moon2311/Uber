from django.contrib import admin

from .models import DriverProfile, Vehicle


class VehicleInline(admin.TabularInline):
    model = Vehicle
    extra = 0


@admin.register(DriverProfile)
class DriverProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "license_number", "availability_status", "rating", "updated_at")
    list_filter = ("availability_status",)
    search_fields = ("user__email", "license_number")
    readonly_fields = ("public_id", "availability_changed_at")
    inlines = [VehicleInline]


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ("plate_number", "make", "model", "category", "driver", "is_active")
    list_filter = ("category", "is_active")
    search_fields = ("plate_number", "driver__user__email")
