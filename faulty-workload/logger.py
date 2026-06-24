"""Structured JSON logger for the faulty-workload service.

Provides a ``JsonFormatter`` that serialises every log record to a single-line
JSON object, and a ``get_logger`` factory that wires a ``StreamHandler``
(stdout) to a named logger using that formatter.

Expected JSON fields per log line
----------------------------------
timestamp  : ISO-8601 UTC timestamp (e.g. "2024-01-15T12:34:56.789012Z")
service    : Logical service name supplied when creating the logger.
severity   : Standard Python level name ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL").
trace_id   : Distributed trace identifier; defaults to "" when not supplied.
request_id : Per-request identifier; defaults to "" when not supplied.
error_type : Machine-readable fault label; defaults to "" when not supplied.
message    : The formatted log message string.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from context import get_request_id, get_trace_id


class JsonFormatter(logging.Formatter):
    """Serialise a :class:`logging.LogRecord` to a single-line JSON string.

    ``trace_id`` and ``request_id`` are resolved in the following order:

    1. An explicit value on the log record (passed via ``extra=``).
    2. The current request-scoped :mod:`contextvars` value (set by middleware).
    3. An empty string when neither is present.

    Parameters
    ----------
    service_name : str
        Value written to the ``service`` field on every record.
    """

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        """Return a JSON-encoded log line for *record*."""
        # ISO-8601 UTC timestamp
        timestamp = (
            datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

        # Prefer explicitly-passed extra values; fall back to context vars.
        trace_id = getattr(record, "trace_id", None) or get_trace_id()
        request_id = getattr(record, "request_id", None) or get_request_id()

        payload = {
            "timestamp": timestamp,
            "service": self._service_name,
            "workload_family": os.environ.get("WORKLOAD_FAMILY", "default"),
            "severity": record.levelname,
            "trace_id": trace_id,
            "request_id": request_id,
            "error_type": getattr(record, "error_type", ""),
            "message": record.getMessage(),
        }

        return json.dumps(payload, ensure_ascii=False)


def get_logger(service_name: str) -> logging.Logger:
    """Return a logger that emits structured JSON to stdout.

    The function is idempotent: calling it multiple times with the same
    *service_name* returns the same :class:`logging.Logger` instance and
    avoids adding duplicate handlers.

    Parameters
    ----------
    service_name : str
        Logical name for the service, written to the ``service`` field of
        every log record.

    Returns
    -------
    logging.Logger
        Configured logger instance.
    """
    logger = logging.getLogger(service_name)

    # Avoid adding duplicate handlers when called more than once.
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name))
    logger.addHandler(handler)

    # Prevent log records from bubbling up to the root logger (which may
    # add its own unformatted output).
    logger.propagate = False

    return logger


# Module-level convenience logger used by app.py via ``from logger import logger``.
# Honour the SERVICE_NAME env var so the container image can be reused for
# different services without rebuilding.
_SERVICE_NAME: str = os.environ.get("SERVICE_NAME", "faulty-workload")
logger: logging.Logger = get_logger(_SERVICE_NAME)
