from django.contrib import admin

from .models import FareQuote, PricingRuleSet, SurgeZone


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PricingRuleSet)
class PricingRuleSetAdmin(ReadOnlyAdmin):
    """Publish new versions with `manage.py pricing_rules publish` (validated)."""

    list_display = ("version", "status", "activated_at", "retired_at", "notes")


@admin.register(FareQuote)
class FareQuoteAdmin(ReadOnlyAdmin):
    list_display = ("id", "rider", "ride_category", "amount", "surge_multiplier", "created_at")
    list_filter = ("ride_category",)


@admin.register(SurgeZone)
class SurgeZoneAdmin(ReadOnlyAdmin):
    list_display = ("zone", "multiplier", "demand", "supply", "rule_set_version", "computed_at")
