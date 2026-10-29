"""Periodic maintenance jobs, in one process.

Run two replicas for availability: a PostgreSQL advisory lock makes exactly one
of them active (the other idles as a hot standby), so jobs never run twice at
once. Each job is isolated: a failing job is logged and counted, never stops the
others. Job outcomes are exported as metrics (last success timestamp per job) so
"the cleanup silently stopped" becomes an alert.
"""

import logging
import time

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import DatabaseError, connection

from apps.common.metrics import metrics
from apps.common.worker import run_loop

logger = logging.getLogger("apps.common")
SCHEDULER_LOCK_ID = 0x0B07_0002

# command -> (interval seconds, args). See docs/operations.md for what each does.
JOBS = {
    "expire_stale_drivers": (60, []),
    "reconcile_live_store": (60, []),
    "prune_live_index": (60, []),
    "cleanup_realtime_presence": (60, []),
    "reconcile_trips": (300, []),  # report-only; findings become metrics / exit code
    "prune_location_history": (86400, []),
    "prune_event_tables": (86400, []),
}


class Command(BaseCommand):
    help = "Run periodic maintenance jobs (single active instance via advisory lock)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run every job once and exit")

    def handle(self, *args, once, **options):
        last_run: dict[str, float] = {}

        def holds_lock() -> bool:
            # Session-level lock: held for this process's connection lifetime.
            with connection.cursor() as c:
                c.execute("SELECT pg_try_advisory_lock(%s)", [SCHEDULER_LOCK_ID])
                return bool(c.fetchone()[0])

        def step():
            if not once and not holds_lock():
                return 0  # standby
            ran = 0
            for name, (interval, job_args) in JOBS.items():
                now = time.monotonic()
                if not once and now - last_run.get(name, -1e9) < interval:
                    continue
                last_run[name] = now
                ran += 1
                start = time.perf_counter()
                try:
                    call_command(name, *job_args, verbosity=0)
                    outcome = "ok"
                except SystemExit as exc:  # commands that signal findings with exit 1
                    outcome = "ok" if not exc.code else "findings"
                except DatabaseError:
                    raise  # run_loop reconnects
                except Exception:  # noqa: BLE001 - isolate jobs from each other
                    logger.exception("scheduler.job_failed", extra={"job": name})
                    outcome = "error"
                elapsed = (time.perf_counter() - start) * 1000
                metrics().timing("scheduler.job_ms", elapsed, tags={"job": name})
                metrics().increment("scheduler.job", tags={"job": name, "outcome": outcome})
                if outcome != "error":
                    metrics().gauge("scheduler.last_success_ts", time.time(), tags={"job": name})
            return 0  # wake up every interval regardless

        if once:
            step()
            return
        try:
            run_loop("scheduler", step, interval=settings.SCHEDULER_TICK_SECONDS)
        finally:
            try:
                connection.close()  # releases the advisory lock
            except DatabaseError:
                pass
