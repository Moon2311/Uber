from django.dispatch import Signal

# kwargs: fix (LocationFix). Sent after a fix was APPLIED to the live store.
# Not transactional (no PostgreSQL write on the hot path); receivers must be cheap
# and must never raise.
driver_location_applied = Signal()
