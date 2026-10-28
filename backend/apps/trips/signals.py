from django.dispatch import Signal

# kwargs: trip
trip_requested = Signal()
# kwargs: trip, from_status, to_status, reason
trip_status_changed = Signal()
