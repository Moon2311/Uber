from django.contrib import admin

from .models import PaymentRecord


@admin.register(PaymentRecord)
class PaymentRecordAdmin(admin.ModelAdmin):
    list_display = ("id", "trip", "rider", "amount", "currency", "status", "method", "created_at")
    list_filter = ("status", "method")
    search_fields = ("id", "trip__id", "rider__email")
    readonly_fields = ("id", "created_at", "updated_at")
