"""Prometheus metrics definitions for the faulty-workload service.

Exposes the following instruments:

Counters
--------
request_count   : Total number of requests received.
warning_count   : Total number of warning-class fault events emitted.
error_count     : Total number of error-class fault events emitted.
timeout_count   : Total number of dependency timeout events simulated.
restart_count   : Total number of pod/process restart signals observed.

Histograms
----------
latency_ms      : Request latency in milliseconds.

Usage
-----
Import the individual metric objects and call ``.inc()`` / ``.observe()`` from
request handlers and fault handlers.  The ``/metrics`` ASGI endpoint is
mounted in ``app.py`` via :func:`make_asgi_app`.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram, make_asgi_app

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

request_count: Counter = Counter(
    "request_count_total",
    "Total number of HTTP requests received by the faulty-workload service.",
)

warning_count: Counter = Counter(
    "warning_count_total",
    "Total number of warning-class fault events emitted (e.g. memory pressure, bad payload).",
    labelnames=["warning_type"],
)

error_count: Counter = Counter(
    "error_count_total",
    "Total number of error-class fault events emitted (e.g. HTTP 500).",
)

timeout_count: Counter = Counter(
    "timeout_count_total",
    "Total number of simulated dependency timeout events.",
)

restart_count: Counter = Counter(
    "restart_count_total",
    "Total number of pod or process restart signals observed.",
)

# ---------------------------------------------------------------------------
# Histograms
# ---------------------------------------------------------------------------

latency_ms: Histogram = Histogram(
    "latency_ms",
    "Request latency in milliseconds.",
    buckets=[10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000],
)

# ---------------------------------------------------------------------------
# ASGI app for /metrics endpoint
# ---------------------------------------------------------------------------

metrics_app = make_asgi_app()
