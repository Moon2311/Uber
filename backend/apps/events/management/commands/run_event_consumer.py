from django.core.management.base import BaseCommand, CommandError

from apps.events.registry import CONSUMER_GROUPS, group_config, physical_group


class Command(BaseCommand):
    help = "Run a consumer group (see apps/events/registry.py)."

    def add_arguments(self, parser):
        parser.add_argument("group", choices=sorted(CONSUMER_GROUPS))
        parser.add_argument("--max-messages", type=int, default=None)
        parser.add_argument("--idle-timeout", type=float, default=None)

    def handle(self, *args, group, max_messages, idle_timeout, **options):
        from apps.events.consumer import ConsumerRunner

        topics, handlers = group_config(group)
        from apps.common.observability import start_worker_metrics_server

        start_worker_metrics_server()
        if not handlers:
            raise CommandError(f"No handlers for {group}")
        ConsumerRunner(physical_group(group), topics, handlers).run(
            max_messages=max_messages, idle_timeout=idle_timeout
        )
