"""Domain signals. Sent synchronously inside the transaction that made the change;
receivers (apps.events) may write to the outbox in that same transaction."""

from django.dispatch import Signal

# kwargs: driver_id, status, previous_status, reason, vehicle_category, changed_at
driver_availability_changed = Signal()
