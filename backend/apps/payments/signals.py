from django.dispatch import Signal

# kwargs: payment, from_status, to_status. Sent INSIDE the transaction that made the
# change (outbox receivers write atomically; WebSocket pushes defer to on_commit).
payment_status_changed = Signal()
