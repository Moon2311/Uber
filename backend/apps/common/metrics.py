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

    def gauge_add(self, name: str, delta: float, tags: Tags = None) -> None:
        """Up/down counter (e.g. open WebSocket connections across processes)."""


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

    def gauge_add(self, name, delta, tags=None):
        with self._lock:
            key = self._key(name, tags)
            self.gauges[key] = self.gauges.get(key, 0) + delta

    def count(self, name, **tags) -> int:
        return self.counters.get(self._key(name, tags or None), 0)


class PrometheusMetrics(MetricsBackend):
    """Prometheus exposition (pull). Works with several gunicorn workers per pod when
    PROMETHEUS_MULTIPROC_DIR is set (prometheus_client multiprocess mode).

    * `a.b.c` becomes `rh_a_b_c` (+ `_total` for counters); timings are histograms
      in milliseconds; gauges report the most recent value of live processes;
      `gauge_add` sums across live processes.
    * Labels are fixed by the FIRST use of a metric name. Later calls with other
      tag keys are coerced (missing -> "", extra dropped) so a call site can never
      crash a request. Unbounded labels (METRICS_DROPPED_LABELS, e.g. H3 zones)
      are always dropped: per-zone data belongs in logs / the database.
    """

    BUCKETS_MS = (1, 2.5, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)

    def __init__(self, registry=None):
        import prometheus_client as pc

        self.registry = registry or pc.REGISTRY
        self._lock = threading.Lock()
        self._metrics: dict[str, tuple[object, tuple[str, ...]]] = {}
        self._dropped = set(getattr(settings, "METRICS_DROPPED_LABELS", ()))

    @staticmethod
    def _name(name: str) -> str:
        return "rh_" + "".join(c if c.isalnum() else "_" for c in name)

    def _get(self, kind: str, name: str, tags: Tags, **kwargs):
        import prometheus_client as pc

        labels = {k: str(v) for k, v in (tags or {}).items() if k not in self._dropped}
        key = f"{kind}:{name}"
        with self._lock:
            entry = self._metrics.get(key)
            if entry is None:
                cls = {"counter": pc.Counter, "histogram": pc.Histogram,
                       "gauge": pc.Gauge, "updown": pc.Gauge}[kind]  # fmt: skip
                label_names = tuple(sorted(labels))
                metric_name = self._name(name) + ("_inflight" if kind == "updown" else "")
                metric = cls(metric_name, name, label_names, registry=self.registry, **kwargs)
                entry = (metric, label_names)
                self._metrics[key] = entry
        metric, label_names = entry
        if not label_names:
            return metric
        return metric.labels(**{n: labels.get(n, "") for n in label_names})

    def increment(self, name, value=1, tags=None):
        self._get("counter", name, tags).inc(value)

    def timing(self, name, milliseconds, tags=None):
        self._get("histogram", name, tags, buckets=self.BUCKETS_MS).observe(milliseconds)

    def gauge(self, name, value, tags=None):
        self._get("gauge", name, tags, multiprocess_mode="livemostrecent").set(value)

    def gauge_add(self, name, delta, tags=None):
        self._get("updown", name, tags, multiprocess_mode="livesum").inc(delta)


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
