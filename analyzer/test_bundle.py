"""Unit tests for analyzer/bundle.py"""

import json
import pytest
from bundle import build_bundle


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


SAMPLE_METRICS = {
    "request_count": 100.0,
    "error_count": 5.0,
    "warning_count": 8.0,
    "timeout_count": 2.0,
    "latency_p50_ms": 10.0,
    "latency_p99_ms": 250.0,
}


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
        bundle = build_bundle(logs, SAMPLE_METRICS)
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
        bundle = build_bundle(logs, SAMPLE_METRICS)
        timestamps = [e["timestamp"] for e in bundle["log_summary"]]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_capped_at_10_entries(self):
        logs = [make_log(f"2024-01-01T00:{i:02d}:00Z", "ERROR") for i in range(15)]
        bundle = build_bundle(logs, SAMPLE_METRICS)
        assert len(bundle["log_summary"]) == 10

    def test_fewer_than_10_logs_kept_as_is(self):
        logs = [make_log(f"2024-01-01T00:0{i}:00Z", "WARNING") for i in range(3)]
        bundle = build_bundle(logs, SAMPLE_METRICS)
        assert len(bundle["log_summary"]) == 3

    def test_empty_logs_returns_empty_list(self):
        bundle = build_bundle([], SAMPLE_METRICS)
        assert bundle["log_summary"] == []

    def test_none_logs_handled(self):
        bundle = build_bundle(None, SAMPLE_METRICS)  # type: ignore[arg-type]
        assert bundle["log_summary"] == []

    def test_mixed_severities_order(self):
        """ERRORs first (most-recent first), then WARNINGs (most-recent first)."""
        logs = [
            make_log("2024-01-01T00:01:00Z", "WARNING", "w1"),
            make_log("2024-01-01T00:02:00Z", "ERROR",   "e1"),
            make_log("2024-01-01T00:03:00Z", "WARNING", "w2"),
            make_log("2024-01-01T00:04:00Z", "ERROR",   "e2"),
        ]
        bundle = build_bundle(logs, SAMPLE_METRICS)
        messages = [e["message"] for e in bundle["log_summary"]]
        assert messages == ["e2", "e1", "w2", "w1"]


# ---------------------------------------------------------------------------
# metric_deltas tests
# ---------------------------------------------------------------------------

class TestMetricDeltas:
    def test_metrics_passed_through_unchanged(self):
        bundle = build_bundle([], SAMPLE_METRICS)
        assert bundle["metric_deltas"] == SAMPLE_METRICS

    def test_none_metrics_returns_empty_dict(self):
        bundle = build_bundle([], None)  # type: ignore[arg-type]
        assert bundle["metric_deltas"] == {}


# ---------------------------------------------------------------------------
# trace_anomalies tests
# ---------------------------------------------------------------------------

class TestTraceAnomalies:
    def test_up_to_5_traces_included(self):
        traces = [{"span_id": f"s{i}", "anomaly": "slow"} for i in range(8)]
        bundle = build_bundle([], SAMPLE_METRICS, traces)
        assert len(bundle["trace_anomalies"]) == 5

    def test_fewer_than_5_traces_kept_as_is(self):
        traces = [{"span_id": "s1"}, {"span_id": "s2"}]
        bundle = build_bundle([], SAMPLE_METRICS, traces)
        assert len(bundle["trace_anomalies"]) == 2

    def test_none_traces_returns_empty_list(self):
        bundle = build_bundle([], SAMPLE_METRICS, None)
        assert bundle["trace_anomalies"] == []

    def test_empty_traces_returns_empty_list(self):
        bundle = build_bundle([], SAMPLE_METRICS, [])
        assert bundle["trace_anomalies"] == []


# ---------------------------------------------------------------------------
# JSON-serialisability test
# ---------------------------------------------------------------------------

class TestJsonSerialisable:
    def test_bundle_is_json_serialisable(self):
        logs = [make_log("2024-01-01T00:00:00Z", "ERROR")]
        traces = [{"span_id": "s1", "anomaly": "timeout"}]
        bundle = build_bundle(logs, SAMPLE_METRICS, traces)
        # Should not raise
        serialised = json.dumps(bundle)
        assert "log_summary" in serialised
        assert "metric_deltas" in serialised
        assert "trace_anomalies" in serialised

    def test_bundle_has_expected_top_level_keys(self):
        bundle = build_bundle([], SAMPLE_METRICS)
        assert set(bundle.keys()) == {"log_summary", "metric_deltas", "trace_anomalies"}
