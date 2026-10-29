import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import DatabaseError, connection

from apps.common.context import bound_request_id
from apps.common.worker import beat
from apps.matching.offers import expire_offers
from apps.matching.orchestrator import MatchingOrchestrator

logger = logging.getLogger("apps.matching")


class Command(BaseCommand):
    help = (
        "Matching worker: expires stale offers and runs matching rounds for due jobs. "
        "Run several replicas safely: jobs are claimed with FOR UPDATE SKIP LOCKED + lease."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, once, **options):
        orchestrator = MatchingOrchestrator()
        from apps.common.observability import start_worker_metrics_server

        start_worker_metrics_server()
        stop = {"flag": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
        signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
        logger.info("matching.worker.started", extra={"owner": orchestrator.owner})
        while not stop["flag"]:
            beat()
            try:
                with bound_request_id(None):  # correlates this round's logs and events
                    expire_offers(locks=orchestrator.state.locks)
                    reports = orchestrator.run_once(limit=settings.MATCHING_WORKER_BATCH)
            except DatabaseError:
                logger.exception("matching.worker.database_error")
                connection.close()
                time.sleep(2)
                continue
            if once:
                break
            if not reports:
                time.sleep(settings.MATCHING_WORKER_POLL_SECONDS)
