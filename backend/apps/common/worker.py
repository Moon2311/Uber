"""Shared poll loop for background workers (surge, payments, notifications).

Each `step()` must be safe to run concurrently in several replicas (they claim
work with SELECT ... FOR UPDATE SKIP LOCKED) and return how much it did; an idle
step sleeps `interval` seconds. Database errors are logged and retried after a
pause with a fresh connection, so a PostgreSQL restart does not kill the worker.
"""

import logging
import signal
import time

from django.db import DatabaseError, connection

logger = logging.getLogger("apps.common")


def run_loop(name: str, step, *, interval: float, once: bool = False) -> None:
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    logger.info(f"{name}.started")
    while not stop["flag"]:
        try:
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
