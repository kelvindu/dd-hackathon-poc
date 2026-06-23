"""
Compact incident bundle builder.

Summarises CloudWatch logs, metrics, and trace anomalies into a small dict
that fits comfortably inside a single Bedrock prompt without sending raw
high-volume telemetry.
"""

from typing import Dict, List, Optional

# Severity ordering – higher number means more severe.
_SEVERITY_RANK: Dict[str, int] = {
    "ERROR": 2,
    "WARNING": 1,
}

_MAX_LOGS = 10
_MAX_TRACES = 5


class _InvertedStr(str):
    """str subclass with reversed comparison operators for descending sort."""

    def __lt__(self, other):  # type: ignore[override]
        return str.__gt__(self, other)

    def __gt__(self, other):  # type: ignore[override]
        return str.__lt__(self, other)

    def __le__(self, other):  # type: ignore[override]
        return str.__ge__(self, other)

    def __ge__(self, other):  # type: ignore[override]
        return str.__le__(self, other)

    def __eq__(self, other):  # type: ignore[override]
        return str.__eq__(self, other)

    def __hash__(self):
        return str.__hash__(self)


def build_bundle(
    logs: List[Dict],
    metrics: Dict[str, float],
    traces: Optional[List[Dict]] = None,
) -> Dict:
    """
    Build a compact incident bundle suitable for inclusion in a Bedrock prompt.

    Args:
        logs:    List of log entry dicts from ``query_logs``.
                 Expected keys: timestamp, severity, error_type, message, trace_id.
        metrics: Metric delta dict from ``query_metrics``.
                 Expected keys: request_count, error_count, warning_count,
                 timeout_count, latency_p50_ms, latency_p99_ms.
        traces:  Optional list of trace anomaly dicts. Only up to 5 are included.

    Returns:
        A JSON-serialisable dict with three keys:
          - log_summary     – top 10 most severe log entries (ERROR before WARNING,
                              then most-recent first within each severity level).
          - metric_deltas   – the metrics dict as-is (key-value pairs).
          - trace_anomalies – up to 5 trace entries (empty list when none given).
    """
    # Sort: primary = severity descending (ERROR first), secondary = timestamp
    # descending (most-recent first within each severity tier).
    # _InvertedStr reverses the lexicographic order so that a later ISO-8601
    # timestamp sorts before an earlier one without needing a numeric conversion.
    sorted_logs = sorted(
        logs or [],
        key=lambda entry: (
            -_SEVERITY_RANK.get(str(entry.get("severity", "")).upper(), 0),
            _InvertedStr(str(entry.get("timestamp", ""))),
        ),
    )

    return {
        "log_summary": sorted_logs[:_MAX_LOGS],
        "metric_deltas": metrics or {},
        "trace_anomalies": (traces or [])[:_MAX_TRACES],
    }
