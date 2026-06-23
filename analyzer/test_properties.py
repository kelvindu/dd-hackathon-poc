"""Property-based tests for datadogllm-poc spec."""

import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

# Add faulty-workload to the path so we can import context & logger modules
_FAULTY_WORKLOAD_DIR = str(Path(__file__).resolve().parent.parent / "faulty-workload")
if _FAULTY_WORKLOAD_DIR not in sys.path:
    sys.path.insert(0, _FAULTY_WORKLOAD_DIR)

from hypothesis import given, settings
from hypothesis import strategies as st

from context import trace_id_var
from logger import JsonFormatter


# ---------------------------------------------------------------------------
# Property 1: Structured log fields are always complete
# ---------------------------------------------------------------------------

REQUIRED_LOG_FIELDS = {"timestamp", "service", "severity", "request_id", "error_type", "message", "trace_id"}


# Feature: datadogllm-poc, Property 1: structured log fields always complete
@given(
    message=st.text(min_size=1),
    severity=st.sampled_from(["DEBUG", "INFO", "WARNING", "ERROR"]),
    extra=st.fixed_dictionaries({
        "error_type": st.text(),
        "trace_id": st.text(),
    }),
)
@settings(max_examples=100)
def test_log_fields_complete(message, severity, extra):
    """For any log record, JsonFormatter must produce JSON with all 7 required fields.

    **Validates: Requirements 1.2**
    """
    with patch("logger.get_trace_id", return_value=extra["trace_id"]), \
         patch("logger.get_request_id", return_value="test-request-id"):
        formatter = JsonFormatter(service_name="test-service")

        level = getattr(logging, severity)
        record = logging.LogRecord(
            name="test",
            level=level,
            pathname="test.py",
            lineno=1,
            msg=message,
            args=None,
            exc_info=None,
        )
        record.error_type = extra["error_type"]
        record.trace_id = extra["trace_id"]

        output = formatter.format(record)
        parsed = json.loads(output)

        missing = REQUIRED_LOG_FIELDS - set(parsed.keys())
        assert not missing, f"Missing fields: {missing}"


# ---------------------------------------------------------------------------
# Property 2: Trace ID context propagation
# ---------------------------------------------------------------------------

# Feature: datadogllm-poc, Property 2: trace ID context propagation
@given(trace_id=st.text(min_size=1, max_size=64))
@settings(max_examples=100)
def test_trace_id_context_propagation(trace_id: str):
    """For any trace_id set in the context variable, the formatted log JSON
    must contain that exact trace_id value.

    **Validates: Requirements 1.3**
    """
    # 1. Set trace_id in the context variable
    token = trace_id_var.set(trace_id)
    try:
        # 2. Create a LogRecord and format it with JsonFormatter
        formatter = JsonFormatter(service_name="test-service")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="test message",
            args=None,
            exc_info=None,
        )
        output = formatter.format(record)

        # 3. Assert output JSON contains the exact trace_id
        parsed = json.loads(output)
        assert parsed["trace_id"] == trace_id, (
            f"Expected trace_id={trace_id!r}, got {parsed['trace_id']!r}"
        )
    finally:
        # Reset context variable to avoid pollution between examples
        trace_id_var.reset(token)
