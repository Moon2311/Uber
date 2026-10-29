from django.contrib import admin

from .models import PaymentAttempt, PaymentRecord, SavedPaymentMethod, WebhookEvent


class ReadOnlyAdmin(admin.ModelAdmin):
    """Money state changes only through apps.payments.services (state machine +
    provider reconciliation), never by editing rows."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PaymentRecord)
class PaymentRecordAdmin(ReadOnlyAdmin):
    list_display = ("id", "trip", "rider", "amount", "currency", "status", "method",
                    "needs_review", "created_at")  # fmt: skip
    list_filter = ("status", "method", "needs_review")
    search_fields = ("id", "trip__id", "rider__email", "provider_reference")


@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(ReadOnlyAdmin):
    list_display = ("idempotency_key", "kind", "status", "amount", "checks", "created_at")
    list_filter = ("kind", "status")


@admin.register(WebhookEvent)
class WebhookEventAdmin(ReadOnlyAdmin):
    list_display = ("provider", "event_id", "event_type", "outcome", "received_at")
    list_filter = ("provider", "outcome")


@admin.register(SavedPaymentMethod)
class SavedPaymentMethodAdmin(ReadOnlyAdmin):
    list_display = ("id", "user", "brand", "last4", "is_default", "is_active")
    exclude = ("provider_token",)  # never displayed
