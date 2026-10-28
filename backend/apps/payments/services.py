from .models import PaymentRecord, PaymentStatus


def create_pending_payment(*, trip) -> PaymentRecord:
    """Idempotent: a trip has exactly one payment record (OneToOne)."""
    payment, _ = PaymentRecord.objects.get_or_create(
        trip=trip,
        defaults={
            "rider": trip.rider,
            "amount": trip.estimated_fare,
            "currency": trip.currency,
            "status": PaymentStatus.PENDING,
        },
    )
    return payment


def cancel_payment_for_trip(*, trip) -> int:
    """Only an un-settled (PENDING) payment is cancelled; anything further along is
    the payments domain's business (refunds etc.). Idempotent."""
    return PaymentRecord.objects.filter(trip=trip, status=PaymentStatus.PENDING).update(
        status=PaymentStatus.CANCELLED
    )
