import json

from django.core.management.base import BaseCommand
from django.db.models import Count

from apps.payments.models import OPEN_ATTEMPT_STATUSES, PaymentAttempt, PaymentRecord


class Command(BaseCommand):
    help = "Payment state counts, open (ambiguous/pending) attempts, and payments needing review."

    def handle(self, *args, **options):
        report = {
            "by_status": dict(PaymentRecord.objects.values_list("status").annotate(n=Count("id"))),
            "open_attempts": dict(
                PaymentAttempt.objects.filter(status__in=OPEN_ATTEMPT_STATUSES)
                .values_list("status")
                .annotate(n=Count("id"))
            ),  # fmt: skip
            "needs_review": list(
                PaymentRecord.objects.filter(needs_review=True).values_list("id", "review_reason")
            ),
        }
        self.stdout.write(json.dumps(report, indent=2, default=str))
        if report["needs_review"]:
            raise SystemExit(1)
