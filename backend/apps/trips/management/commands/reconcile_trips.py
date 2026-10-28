import json

from django.core.management.base import BaseCommand

from apps.trips.reconciliation import reconcile


class Command(BaseCommand):
    help = (
        "Check trips against their audit log, matching job, offers, payment record and "
        "outbox. --fix repairs what can be repaired safely (each repair re-checks under "
        "the trip lock). Exits 1 if anything is left unrepaired. Schedule every few minutes."
    )

    def add_arguments(self, parser):
        parser.add_argument("--fix", action="store_true")
        parser.add_argument("--hours", type=float, default=48, help="Only trips updated recently")
        parser.add_argument("--all", action="store_true", help="Scan every trip")

    def handle(self, *args, fix, hours, all, **options):  # noqa: A002
        report = reconcile(fix=fix, hours=None if all else hours)
        self.stdout.write(json.dumps(report.summary(), indent=2, sort_keys=True))
        unrepaired = {
            check: sorted(set(ids) - set(report.repaired.get(check, [])))
            for check, ids in report.found.items()
        }
        unrepaired = {k: v[:20] for k, v in unrepaired.items() if v}
        if unrepaired:
            self.stderr.write("Unrepaired (first 20 per check): " + json.dumps(unrepaired))
            raise SystemExit(1)
