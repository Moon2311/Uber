from django.dispatch import Signal

# Both are sent INSIDE the transaction that made the change; receivers that write
# (outbox, payments, matching) therefore commit or roll back with it. Receivers
# that talk to the outside world must defer with transaction.on_commit.

# kwargs: trip, actor_type, event_id (audit/outbox correlation id)
trip_requested = Signal()
# kwargs: trip, from_status, to_status, reason, actor_type, previous_driver_id, event_id
trip_status_changed = Signal()
