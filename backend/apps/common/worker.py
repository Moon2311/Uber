"""Shared poll loop for background workers (surge, payments, notifications).

Each `step()` must be safe to run concurrently in several replicas (they claim
work with SELECT ... FOR UPDATE SKIP LOCKED) and return how much it did; an idle
step sleeps `interval` seconds. Database errors are logged and retried after a
pause with a fresh connection, so a PostgreSQL restart does not kill the worker.
"""

import logging
import os
import signal
import time

from django.db import DatabaseError, connection

from .context import bound_request_id

logger = logging.getLogger("apps.common")


def beat() -> None:
    """Liveness heartbeat for healthcheck.py: touched once per loop iteration."""
    path = os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/worker-heartbeat")  # noqa: S108
    try:
        with open(path, "a"):
            os.utime(path, None)
    except OSError:
        pass


def run_loop(name: str, step, *, interval: float, once: bool = False) -> None:
    if not once:
        from .observability import start_worker_metrics_server

        start_worker_metrics_server()
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    logger.info(f"{name}.started")
    while not stop["flag"]:
        beat()
        try:
            with bound_request_id(None):  # one correlation id per iteration
                done = step()
        except DatabaseError:
            logger.exception(f"{name}.database_error")
            connection.close()
            time.sleep(2)
            continue
        if once:
            break
        if not done:
            time.sleep(interval)
    logger.info(f"{name}.stopped")
