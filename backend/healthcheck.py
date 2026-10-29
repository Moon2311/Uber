"""Container health probe (Docker HEALTHCHECK and Kubernetes exec probes).

web / asgi : GET /api/v1/health/live/ on the local port.
workers    : the worker loop touches HEARTBEAT_FILE every iteration; healthy if it
             was touched recently. A wedged loop (deadlock, hung call without a
             timeout) therefore fails the probe even though the process is alive.
"""

import os
import sys
import time
import urllib.request

HEARTBEAT_FILE = os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/worker-heartbeat")  # noqa: S108


def main() -> int:
    cmd = open("/proc/1/cmdline", "rb").read().split(b"\0")
    if any(b"gunicorn" in part for part in cmd) or any(b"daphne" in part for part in cmd):
        port = os.environ.get("PORT", "8001" if any(b"daphne" in p for p in cmd) else "8000")
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health/live/", timeout=2)  # noqa: S310
        return 0
    max_age = float(os.environ.get("WORKER_HEARTBEAT_MAX_AGE", "120"))
    try:
        age = time.time() - os.path.getmtime(HEARTBEAT_FILE)
    except FileNotFoundError:
        return 1
    return 0 if age <= max_age else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        sys.exit(1)
