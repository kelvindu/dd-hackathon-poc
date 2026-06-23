"""Unit tests for _normalize_logs() and _normalize_traces() helpers."""

import sys
from unittest.mock import MagicMock

# Mock the mcp module and its submodules before importing datadog_mcp
mock_mcp = MagicMock()
sys.modules["mcp"] = mock_mcp
sys.modules["mcp.client"] = mock_mcp.client
sys.modules["mcp.client.sse"] = mock_mcp.client.sse
sys.modules["mcp.client.stdio"] = mock_mcp.client.stdio

import pytest  # noqa: E402

from datadog_mcp import _normalize_logs, _normalize_traces  # noqa: E402


class TestNormalizeLogs:
    """Tests for _normalize_logs()."""

    def test_full_log_entry(self):
        """Normalizes a complete Datadog MCP log event."""
        raw = {
            "data": [
                {
                    "id": "abc123",
                    "attributes": {
                        "timestamp": "2024-06-01T12:00:00Z",
                        "status": "error",
                        "message": "Connection refused",
                        "tags": ["service:faulty-workload", "trace_id:tr-001"],
                        "attributes": {
                            "error": {"kind": "http_exception"},
                        },
                    },
                }
            ]
        }
        result = _normalize_logs(raw)
        assert len(result) == 1
        entry = result[0]
        assert entry["timestamp"] == "2024-06-01T12:00:00Z"
        assert entry["severity"] == "ERROR"
        assert entry["error_type"] == "http_exception"
        assert entry["message"] == "Connection refused"
        assert entry["trace_id"] == "tr-001"
        assert entry["service"] == "faulty-workload"

    def test_plain_list_input(self):
        """Accepts a plain list of log events (no wrapper)."""
        raw = [
            {
                "attributes": {
                    "timestamp": "2024-06-01T13:00:00Z",
                    "status": "warn",
                    "message": "High latency detected",
                    "tags": ["service:analyzer"],
                    "attributes": {},
                }
            }
        ]
        result = _normalize_logs(raw)
        assert len(result) == 1
        assert result[0]["severity"] == "WARN"
        assert result[0]["service"] == "analyzer"

    def test_missing_fields_default_to_empty(self):
        """Missing fields gracefully default to empty strings."""
        raw = [{"attributes": {}}]
        result = _normalize_logs(raw)
        assert len(result) == 1
        entry = result[0]
        assert entry["timestamp"] == ""
        assert entry["severity"] == "INFO"  # default when status is empty
        assert entry["error_type"] == ""
        assert entry["message"] == ""
        assert entry["trace_id"] == ""
        assert entry["service"] == ""

    def test_none_input_returns_empty(self):
        """None input returns an empty list."""
        assert _normalize_logs(None) == []

    def test_empty_dict_returns_empty(self):
        """Empty dict returns an empty list."""
        assert _normalize_logs({}) == []

    def test_severity_mapping(self):
        """Maps various Datadog status values to standard severity labels."""
        cases = [
            ("error", "ERROR"),
            ("err", "ERROR"),
            ("warn", "WARN"),
            ("warning", "WARN"),
            ("info", "INFO"),
            ("debug", "DEBUG"),
            ("ok", "INFO"),
        ]
        for raw_status, expected in cases:
            raw = [{"attributes": {"status": raw_status}}]
            result = _normalize_logs(raw)
            assert result[0]["severity"] == expected, f"Failed for status={raw_status}"

    def test_trace_id_from_nested_attrs(self):
        """Falls back to trace_id from nested attributes when not in tags."""
        raw = [
            {
                "attributes": {
                    "timestamp": "2024-06-01T14:00:00Z",
                    "status": "info",
                    "message": "ok",
                    "tags": [],
                    "attributes": {"trace_id": "nested-trace-123"},
                }
            }
        ]
        result = _normalize_logs(raw)
        assert result[0]["trace_id"] == "nested-trace-123"

    def test_non_dict_events_skipped(self):
        """Non-dict entries in the list are skipped."""
        raw = [None, "bad", 123, {"attributes": {"message": "good"}}]
        result = _normalize_logs(raw)
        assert len(result) == 1
        assert result[0]["message"] == "good"


class TestNormalizeTraces:
    """Tests for _normalize_traces()."""

    def test_full_trace_entry(self):
        """Normalizes a complete Datadog APM trace span."""
        raw = {
            "data": [
                {
                    "attributes": {
                        "trace_id": "tr-abc",
                        "span_id": "sp-001",
                        "service": "faulty-workload",
                        "resource": "GET /health",
                        "error": True,
                        "duration": 5000000,  # 5ms in nanoseconds
                        "start": "2024-06-01T12:00:00Z",
                        "meta": {
                            "error": {
                                "type": "http_exception",
                                "message": "500 Internal Server Error",
                            }
                        },
                    }
                }
            ]
        }
        result = _normalize_traces(raw)
        assert len(result) == 1
        span = result[0]
        assert span["trace_id"] == "tr-abc"
        assert span["span_id"] == "sp-001"
        assert span["service"] == "faulty-workload"
        assert span["resource"] == "GET /health"
        assert span["error"] is True
        assert span["duration"] == 5.0  # 5ms
        assert span["start"] == "2024-06-01T12:00:00Z"
        assert span["error_type"] == "http_exception"
        assert span["error_message"] == "500 Internal Server Error"

    def test_plain_list_input(self):
        """Accepts a plain list of span dicts (no wrapper)."""
        raw = [
            {
                "attributes": {
                    "trace_id": "tr-xyz",
                    "span_id": "sp-002",
                    "service": "analyzer",
                    "resource": "POST /rca/analyze",
                    "duration": 10000000,  # 10ms
                }
            }
        ]
        result = _normalize_traces(raw)
        assert len(result) == 1
        assert result[0]["service"] == "analyzer"
        assert result[0]["resource"] == "POST /rca/analyze"
        assert result[0]["duration"] == 10.0

    def test_missing_fields_default_gracefully(self):
        """Missing fields default to empty strings, 0.0, or False."""
        raw = [{"attributes": {}}]
        result = _normalize_traces(raw)
        assert len(result) == 1
        span = result[0]
        assert span["trace_id"] == ""
        assert span["span_id"] == ""
        assert span["service"] == ""
        assert span["resource"] == ""
        assert span["error"] is False
        assert span["duration"] == 0.0
        assert span["start"] == ""
        assert span["error_type"] == ""
        assert span["error_message"] == ""

    def test_none_input_returns_empty(self):
        """None input returns an empty list."""
        assert _normalize_traces(None) == []

    def test_empty_dict_returns_empty(self):
        """Empty dict returns an empty list."""
        assert _normalize_traces({}) == []

    def test_wrapper_with_traces_key(self):
        """Accepts wrapper dict with 'traces' key."""
        raw = {
            "traces": [
                {
                    "attributes": {
                        "trace_id": "tr-from-traces-key",
                        "service": "svc",
                        "resource": "op",
                        "duration": 1000000,
                    }
                }
            ]
        }
        result = _normalize_traces(raw)
        assert len(result) == 1
        assert result[0]["trace_id"] == "tr-from-traces-key"

    def test_wrapper_with_spans_key(self):
        """Accepts wrapper dict with 'spans' key."""
        raw = {
            "spans": [
                {
                    "attributes": {
                        "trace_id": "tr-from-spans-key",
                        "service": "svc2",
                        "resource": "op2",
                        "duration": 2000000,
                    }
                }
            ]
        }
        result = _normalize_traces(raw)
        assert len(result) == 1
        assert result[0]["trace_id"] == "tr-from-spans-key"

    def test_non_numeric_duration(self):
        """Non-numeric duration defaults to 0.0."""
        raw = [{"attributes": {"duration": "not-a-number"}}]
        result = _normalize_traces(raw)
        assert result[0]["duration"] == 0.0

    def test_non_dict_spans_skipped(self):
        """Non-dict entries in the list are skipped."""
        raw = [None, "bad", {"attributes": {"trace_id": "good"}}]
        result = _normalize_traces(raw)
        assert len(result) == 1
        assert result[0]["trace_id"] == "good"
