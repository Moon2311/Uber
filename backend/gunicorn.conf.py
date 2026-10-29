"""Gunicorn settings for the HTTP API (env-overridable).

Sync workers: requests are short and PostgreSQL-bound; concurrency comes from
more workers per pod and more pods (horizontal scaling), not threads.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("GUNICORN_WORKERS", "3"))
# A request stuck longer than this is killed (the DB statement_timeout is lower,
# so this only fires for code bugs or a hung dependency without its own timeout).
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "30"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "25"))  # < k8s grace period
keepalive = 5  # seconds an idle keep-alive connection from the load balancer stays open
backlog = int(os.environ.get("GUNICORN_BACKLOG", "256"))  # bounded accept queue (backpressure)
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "2000"))  # recycle: caps leaks
max_requests_jitter = 200
accesslog = None  # RequestContextMiddleware logs slow/failed requests with request ids
errorlog = "-"
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
worker_tmp_dir = os.environ.get("GUNICORN_TMP_DIR", "/tmp")  # noqa: S108 - tmpfs in k8s


def child_exit(server, worker):
    # Drop the dead worker's Prometheus multiprocess files (live* gauges, counters
    # are kept by the collector as intended).
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
