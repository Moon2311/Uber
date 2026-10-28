from django.contrib import admin

from .models import MatchingJob, RideOffer


@admin.register(RideOffer)
class RideOfferAdmin(admin.ModelAdmin):
    list_display = ("id", "trip", "driver", "status", "rank", "pickup_eta_seconds", "eta_source",
                    "expires_at")  # fmt: skip
    list_filter = ("status", "eta_source")


@admin.register(MatchingJob)
class MatchingJobAdmin(admin.ModelAdmin):
    list_display = ("trip", "status", "rounds", "offers_made", "next_run_at", "deadline",
                    "lease_owner")  # fmt: skip
    list_filter = ("status",)
