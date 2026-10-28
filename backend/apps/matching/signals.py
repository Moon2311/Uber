from django.dispatch import Signal

# kwargs: offer. Sent inside the transaction that created/accepted the offer.
ride_offer_created = Signal()
ride_offer_accepted = Signal()
# kwargs: offer. Sent when a PENDING offer is closed without acceptance (declined,
# expired, cancelled). Sent inside the closing transaction.
ride_offer_closed = Signal()
