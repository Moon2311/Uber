from django.contrib import admin, messages
from django.utils import timezone

from .models import CaseStatus, FraudCase, FraudDecision, RiskProfile, RiskSignal


@admin.register(FraudCase)
class FraudCaseAdmin(admin.ModelAdmin):
    """Human review: the only place a hold is placed or lifted."""

    list_display = ("id", "subject_type", "subject_id", "status", "score_at_open", "reason",
                    "opened_at")  # fmt: skip
    list_filter = ("status", "subject_type")
    readonly_fields = ("subject_type", "subject_id", "reason", "score_at_open", "opened_at",
                       "resolved_at", "resolved_by")  # fmt: skip
    actions = ["place_hold", "dismiss"]

    def _resolve(self, request, queryset, status, hold):
        for case in queryset.filter(status=CaseStatus.OPEN):
            RiskProfile.objects.update_or_create(
                subject_type=case.subject_type, subject_id=case.subject_id,
                defaults={"hold": hold, "hold_reason": f"case {case.pk}" if hold else ""},
            )  # fmt: skip
            case.status, case.resolved_at, case.resolved_by = status, timezone.now(), request.user
            case.save(update_fields=["status", "resolved_at", "resolved_by"])
        messages.success(request, "Updated.")

    @admin.action(description="Place a hold (blocks sensitive actions)")
    def place_hold(self, request, queryset):
        self._resolve(request, queryset, CaseStatus.ACTIONED, True)

    @admin.action(description="Dismiss (no action; lifts any hold)")
    def dismiss(self, request, queryset):
        self._resolve(request, queryset, CaseStatus.DISMISSED, False)


@admin.register(RiskProfile)
class RiskProfileAdmin(admin.ModelAdmin):
    list_display = ("subject_type", "subject_id", "score", "blocking_score", "level", "hold")
    list_filter = ("level", "hold", "subject_type")
    readonly_fields = ("subject_type", "subject_id", "score", "blocking_score", "level",
                       "signals")  # fmt: skip


@admin.register(RiskSignal)
class RiskSignalAdmin(admin.ModelAdmin):
    list_display = ("subject_type", "subject_id", "signal", "reliability", "weight", "observed_at")
    list_filter = ("signal", "reliability")

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(FraudDecision)
class FraudDecisionAdmin(admin.ModelAdmin):
    list_display = ("action", "subject_type", "subject_id", "decision", "score", "created_at")
    list_filter = ("action", "decision")

    def has_change_permission(self, request, obj=None):
        return False
