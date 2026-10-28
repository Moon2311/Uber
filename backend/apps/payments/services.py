from .models import PaymentRecord, PaymentStatus


def create_pending_payment(*, trip) -> PaymentRecord:
    return PaymentRecord.objects.create(
        trip=trip,
        rider=trip.rider,
        amount=trip.estimated_fare,
        currency=trip.currency,
        status=PaymentStatus.PENDING,
    )


def cancel_payment_for_trip(*, trip) -> None:
    PaymentRecord.objects.filter(trip=trip, status=PaymentStatus.PENDING).update(
        status=PaymentStatus.CANCELLED
    )
