import json

from django.core.management.base import BaseCommand, CommandError

from apps.pricing.models import PricingRuleSet
from apps.pricing.rules import RuleError
from apps.pricing.services import active_rule_set, publish_rule_set


class Command(BaseCommand):
    help = "List, show or publish versioned pricing rules (published versions are immutable)."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("list")
        show = sub.add_parser("show")
        show.add_argument("version", nargs="?", type=int)
        publish = sub.add_parser("publish")
        publish.add_argument("file", help="JSON rules document")
        publish.add_argument("--notes", default="")

    def handle(self, *args, action, **options):
        if action == "list":
            active_rule_set()  # bootstraps v1 on a fresh database
            for row in PricingRuleSet.objects.order_by("version"):
                self.stdout.write(f"v{row.version}\t{row.status}\t{row.activated_at:%Y-%m-%d %H:%M}"
                                  f"\t{row.notes}")  # fmt: skip
        elif action == "show":
            row = (
                PricingRuleSet.objects.get(version=options["version"])
                if options["version"]
                else active_rule_set()[0]
            )
            self.stdout.write(json.dumps(row.rules, indent=2))
        else:
            with open(options["file"]) as fh:
                raw = json.load(fh)
            try:
                row = publish_rule_set(raw, notes=options["notes"])
            except RuleError as exc:
                raise CommandError(f"Invalid rules: {exc}") from exc
            self.stdout.write(f"Published pricing v{row.version} (now ACTIVE)")
