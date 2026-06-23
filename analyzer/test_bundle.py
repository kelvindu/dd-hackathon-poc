"""Unit tests for analyzer/bundle.py"""

import sys
from unittest.mock import MagicMock

# Mock the mcp module and its submodules before importing bundle/datadog_mcp
mock_mcp = MagicMock()
sys.modules["mcp"] = mock_mcp
sys.modules["mcp.client"] = mock_mcp.client
sys.modules["mcp.client.sse"] = mock_mcp.client.sse
sys.modules["mcp.client.stdio"] = mock_mcp.client.stdio

import json  # noqa: E402
import pytest  # noqa: E402
from bundle import build_bundle  # noqa: E402
from datadog_mcp import MCPEvidence  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def make_log(timestamp: str, severity: str, message: str = "test") -> dict:
    return {
        "timestamp": timestamp,
        "severity": severity,
        "error_type": "TEST",
        "message": message,
        "trace_id": "abc123",
    }


def make_evidence(
    logs=None, traces=None, monitors=None, incidents=None
) -> MCPEvidence:
    return MCPEvidence(
        logs=logs or [],
        traces=traces or [],
        monitors=monitors or [],
        incidents=incidents or [],
    )


# ---------------------------------------------------------------------------
# log_summary tests
# ---------------------------------------------------------------------------

class TestLogSummary:
    def test_errors_come_before_warnings(self):
        logs = [
            make_log("2024-01-01T00:01:00Z", "WARNING"),
            make_log("2024-01-01T00:02:00Z", "ERROR"),
            make_log("2024-01-01T00:03:00Z", "WARNING"),
        ]
        bundle = build_bundle(make_evidence(logs=logs))
        severities = [e["severity"] for e in bundle["log_summary"]]
        # All ERRORs must appear before any WARNING
        first_warning = next(i for i, s in enumerate(severities) if s == "WARNING")
        assert all(s == "ERROR" for s in severities[:first_warning])

    def test_most_recent_first_within_same_severity(self):
        logs = [
            make_log("2024-01-01T00:01:00Z", "ERROR"),
            make_log("2024-01-01T00:03:00Z", "ERROR"),
            make_log("2024-01-01T00:02:00Z", "ERROR"),
        ]
        bundle = build_bundle(make_evidence(logs=logs))
        timestamps = [e["timestamp"] for e in bundle["log_summary"]]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_capped_at_10_entries(self):
        logs = [make_log(f"2024-01-01T00:{i:02d}:00Z", "ERROR") for i in range(15)]
        bundle = build_bundle(make_evidence(logs=logs))
        assert len(bundle["log_summary"]) == 10

    def test_fewer_than_10_logs_kept_as_is(self):
        logs = [make_log(f"2024-01-01T00:0{i}:00Z", "WARNING") for i in range(3)]
        bundle = build_bundle(make_evidence(logs=logs))
        assert len(bundle["log_summary"]) == 3

    def test_empty_logs_returns_empty_list(self):
        bundle = build_bundle(make_evidence())
        assert bundle["log_summary"] == []

    def test_mixed_severities_order(self):
        """ERRORs first (most-recent first), then WARNINGs (most-recent first)."""
        logs = [
            make_log("2024-01-01T00:01:00Z", "WARNING", "w1"),
            make_log("2024-01-01T00:02:00Z", "ERROR",   "e1"),
            make_log("2024-01-01T00:03:00Z", "WARNING", "w2"),
            make_log("2024-01-01T00:04:00Z", "ERROR",   "e2"),
        ]
        bundle = build_bundle(make_evidence(logs=logs))
        messages = [e["message"] for e in bundle["log_summary"]]
        assert messages == ["e2", "e1", "w2", "w1"]


# ---------------------------------------------------------------------------
# trace_anomalies tests
# ---------------------------------------------------------------------------

class TestTraceAnomalies:
    def test_up_to_5_traces_included(self):
        traces = [{"span_id": f"s{i}", "anomaly": "slow"} for i in range(8)]
        bundle = build_bundle(make_evidence(traces=traces))
        assert len(bundle["trace_anomalies"]) == 5

    def test_fewer_than_5_traces_kept_as_is(self):
        traces = [{"span_id": "s1"}, {"span_id": "s2"}]
        bundle = build_bundle(make_evidence(traces=traces))
        assert len(bundle["trace_anomalies"]) == 2

    def test_empty_traces_returns_empty_list(self):
        bundle = build_bundle(make_evidence())
        assert bundle["trace_anomalies"] == []


# ---------------------------------------------------------------------------
# monitor_alerts tests
# ---------------------------------------------------------------------------

class TestMonitorAlerts:
    def test_up_to_5_monitors_included(self):
        monitors = [{"id": f"m{i}", "state": "Alert"} for i in range(8)]
        bundle = build_bundle(make_evidence(monitors=monitors))
        assert len(bundle["monitor_alerts"]) == 5

    def test_fewer_than_5_monitors_kept_as_is(self):
        monitors = [{"id": "m1", "state": "Warn"}]
        bundle = build_bundle(make_evidence(monitors=monitors))
        assert len(bundle["monitor_alerts"]) == 1

    def test_empty_monitors_returns_empty_list(self):
        bundle = build_bundle(make_evidence())
        assert bundle["monitor_alerts"] == []


# ---------------------------------------------------------------------------
# incidents tests
# ---------------------------------------------------------------------------

class TestIncidents:
    def test_up_to_3_incidents_included(self):
        incidents = [{"id": f"inc{i}", "state": "active"} for i in range(6)]
        bundle = build_bundle(make_evidence(incidents=incidents))
        assert len(bundle["incidents"]) == 3

    def test_fewer_than_3_incidents_kept_as_is(self):
        incidents = [{"id": "inc1", "state": "active"}]
        bundle = build_bundle(make_evidence(incidents=incidents))
        assert len(bundle["incidents"]) == 1

    def test_empty_incidents_returns_empty_list(self):
        bundle = build_bundle(make_evidence())
        assert bundle["incidents"] == []


# ---------------------------------------------------------------------------
# JSON-serialisability test
# ---------------------------------------------------------------------------

class TestJsonSerialisable:
    def test_bundle_is_json_serialisable(self):
        logs = [make_log("2024-01-01T00:00:00Z", "ERROR")]
        traces = [{"span_id": "s1", "anomaly": "timeout"}]
        monitors = [{"id": "m1", "state": "Alert"}]
        incidents = [{"id": "inc1", "state": "active"}]
        bundle = build_bundle(make_evidence(
            logs=logs, traces=traces, monitors=monitors, incidents=incidents
        ))
        # Should not raise
        serialised = json.dumps(bundle)
        assert "log_summary" in serialised
        assert "trace_anomalies" in serialised
        assert "monitor_alerts" in serialised
        assert "incidents" in serialised

    def test_bundle_has_expected_top_level_keys(self):
        bundle = build_bundle(make_evidence())
        assert set(bundle.keys()) == {
            "log_summary", "trace_anomalies", "monitor_alerts", "incidents"
        }
