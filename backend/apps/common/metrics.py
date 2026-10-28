"""Pluggable metrics hooks.

Application code calls `metrics().increment(...)` / `.timing(...)` / `.gauge(...)`.
The backend is selected via settings.METRICS_BACKEND (dotted path), so StatsD,
Prometheus or OpenTelemetry can be wired in later without touching call sites.
"""

import logging
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from functools import lru_cache

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger("metrics")

Tags = dict[str, str] | None


class MetricsBackend:
    def increment(self, name: str, value: int = 1, tags: Tags = None) -> None:
        raise NotImplementedError

    def timing(self, name: str, milliseconds: float, tags: Tags = None) -> None:
        raise NotImplementedError

    def gauge(self, name: str, value: float, tags: Tags = None) -> None:
        raise NotImplementedError


class NoopMetrics(MetricsBackend):
    def increment(self, name, value=1, tags=None):
        pass

    def timing(self, name, milliseconds, tags=None):
        pass

    def gauge(self, name, value, tags=None):
        pass


class LoggingMetrics(MetricsBackend):
    """Emits metrics as structured DEBUG log lines (useful in development)."""

    def _emit(self, kind, name, value, tags):
        logger.debug(
            "metric",
            extra={"metric_type": kind, "metric": name, "value": value, "tags": tags or {}},
        )

    def increment(self, name, value=1, tags=None):
        self._emit("counter", name, value, tags)

    def timing(self, name, milliseconds, tags=None):
        self._emit("timing", name, round(milliseconds, 3), tags)

    def gauge(self, name, value, tags=None):
        self._emit("gauge", name, value, tags)


class InMemoryMetrics(MetricsBackend):
    """Thread-safe in-process recorder; used by tests and handy for debugging."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    @staticmethod
    def _key(name, tags):
        return (name, tuple(sorted((tags or {}).items())))

    def reset(self):
        self.counters = defaultdict(int)
        self.timings = defaultdict(list)
        self.gauges = {}

    def increment(self, name, value=1, tags=None):
        with self._lock:
            self.counters[self._key(name, tags)] += value

    def timing(self, name, milliseconds, tags=None):
        with self._lock:
            self.timings[self._key(name, tags)].append(milliseconds)

    def gauge(self, name, value, tags=None):
        with self._lock:
            self.gauges[self._key(name, tags)] = value

    def count(self, name, **tags) -> int:
        return self.counters.get(self._key(name, tags or None), 0)


@lru_cache(maxsize=1)
def metrics() -> MetricsBackend:
    return import_string(settings.METRICS_BACKEND)()


@contextmanager
def timed(name: str, tags: Tags = None):
    start = time.perf_counter()
    try:
        yield
    finally:
        metrics().timing(name, (time.perf_counter() - start) * 1000, tags)
