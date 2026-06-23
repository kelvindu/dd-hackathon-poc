"""Request-scoped context variables for trace and request ID propagation.

Uses Python's :mod:`contextvars` so each async task (i.e. each FastAPI
request) has its own isolated values without any thread-local or global state.

Usage
-----
Set values at the start of every request (typically in middleware)::

    from context import trace_id_var, request_id_var
    trace_id_var.set("abc-123")
    request_id_var.set("req-456")

Read values anywhere downstream — in log formatters, metric helpers, etc. —
without passing them explicitly::

    from context import get_trace_id, get_request_id
    tid = get_trace_id()   # "abc-123"  (or "" if not set)
    rid = get_request_id() # "req-456"  (or "" if not set)
"""

from __future__ import annotations

from contextvars import ContextVar

# ---------------------------------------------------------------------------
# ContextVar definitions
# ---------------------------------------------------------------------------

#: Distributed trace identifier.  Set per-request from the ``X-Trace-ID``
#: header or a freshly generated UUID.
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")

#: Per-request identifier.  Set from the ``X-Request-ID`` header or a
#: freshly generated UUID.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


# ---------------------------------------------------------------------------
# Convenience accessors
# ---------------------------------------------------------------------------


def get_trace_id() -> str:
    """Return the trace ID for the current request context."""
    return trace_id_var.get()


def get_request_id() -> str:
    """Return the request ID for the current request context."""
    return request_id_var.get()
