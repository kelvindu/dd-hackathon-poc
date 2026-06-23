"""
Compact incident bundle builder.

Summarises Datadog logs, traces, monitor alerts, and incidents into a small
dict that fits comfortably inside a single Bedrock prompt without sending raw
high-volume telemetry.
"""

from typing import Dict

from datadog_mcp import MCPEvidence

# Severity ordering – higher number means more severe.
_SEVERITY_RANK: Dict[str, int] = {
    "ERROR": 2,
    "WARNING": 1,
}

_MAX_LOGS = 10
_MAX_TRACES = 5
_MAX_MONITORS = 5
_MAX_INCIDENTS = 3


class _InvertedStr(str):
    """str subclass with reversed comparison operators for descending sort."""

    def __lt__(self, other):
        return str.__gt__(self, other)

    def __gt__(self, other):
        return str.__lt__(self, other)

    def __le__(self, other):
        return str.__ge__(self, other)

    def __ge__(self, other):
        return str.__le__(self, other)

    def __eq__(self, other):
        return str.__eq__(self, other)

    def __hash__(self):
        return str.__hash__(self)


def build_bundle(evidence: MCPEvidence) -> dict:
    """
    Build a compact incident bundle from MCPEvidence.

    Returns a dict with:
      log_summary     – top 10 most severe log entries
      trace_anomalies – up to 5 error traces
      monitor_alerts  – up to 5 monitors in alert/warn
      incidents       – up to 3 active incidents
    """
    sorted_logs = sorted(
        evidence.logs or [],
        key=lambda entry: (
            -_SEVERITY_RANK.get(str(entry.get("severity", "")).upper(), 0),
            _InvertedStr(str(entry.get("timestamp", ""))),
        ),
    )

    return {
        "log_summary": sorted_logs[:_MAX_LOGS],
        "trace_anomalies": (evidence.traces or [])[:_MAX_TRACES],
        "monitor_alerts": (evidence.monitors or [])[:_MAX_MONITORS],
        "incidents": (evidence.incidents or [])[:_MAX_INCIDENTS],
    }
