"""Datadog MCP evidence retrieval layer.

Replaces cloudwatch.py — all incident evidence is fetched exclusively
via Datadog MCP server calls.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client


@dataclass
class MCPEvidence:
    """Structured evidence collected from Datadog via MCP tool calls."""

    logs: list[dict[str, Any]] = field(default_factory=list)
    traces: list[dict[str, Any]] = field(default_factory=list)
    monitors: list[dict[str, Any]] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)
    dashboards: list[dict[str, Any]] = field(default_factory=list)


class MCPUnavailableError(RuntimeError):
    """Raised when the MCP server process cannot be started or reached."""


class MCPQueryError(RuntimeError):
    """Raised when an individual MCP tool call fails."""


@asynccontextmanager
async def _get_mcp_client() -> AsyncIterator[ClientSession]:
    """Yield an initialized MCP ClientSession ready for tool calls.

    Reads DD_MCP_TRANSPORT env var to choose transport:
      - "stdio" (default): spawns `npx -y @datadog/mcp` with DD credentials
      - "http": connects to DD_MCP_URL via SSE

    Usage::

        async with _get_mcp_client() as session:
            result = await session.call_tool("tool_name", arguments={...})

    Raises:
        MCPUnavailableError: When required env vars are missing or connection fails.
    """
    transport = os.environ.get("DD_MCP_TRANSPORT", "stdio")

    async with AsyncExitStack() as stack:
        try:
            if transport == "stdio":
                # Validate required env vars for stdio transport
                try:
                    api_key = os.environ["DD_API_KEY"]
                    app_key = os.environ["DD_APP_KEY"]
                except KeyError as exc:
                    raise MCPUnavailableError(
                        f"Missing required environment variable for MCP stdio transport: {exc}"
                    ) from exc

                server_params = StdioServerParameters(
                    command="npx",
                    args=["-y", "@datadog/mcp"],
                    env={
                        "DD_API_KEY": api_key,
                        "DD_APP_KEY": app_key,
                        "DD_SITE": os.environ.get("DD_SITE", "datadoghq.com"),
                    },
                )

                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(server_params)
                )
            else:
                # HTTP/SSE transport
                url = os.environ.get("DD_MCP_URL", "http://localhost:3000")
                read_stream, write_stream = await stack.enter_async_context(
                    sse_client(url)
                )

            # Create and initialize the ClientSession
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()

            yield session

        except MCPUnavailableError:
            raise
        except Exception as exc:
            if transport == "stdio":
                raise MCPUnavailableError(
                    f"Failed to start MCP stdio subprocess: {exc}"
                ) from exc
            else:
                url = os.environ.get("DD_MCP_URL", "http://localhost:3000")
                raise MCPUnavailableError(
                    f"Failed to connect to MCP server at {url}: {exc}"
                ) from exc


def _normalize_logs(raw: dict[str, Any] | list | None) -> list[dict[str, Any]]:
    """Map raw MCP log event dicts to a consistent normalized shape.

    Expected raw structure from Datadog MCP logs_list_events:
        {
            "id": "...",
            "attributes": {
                "timestamp": "2024-01-01T00:00:00Z",
                "status": "error",
                "message": "...",
                "tags": ["service:faulty-workload", "trace_id:abc123"],
                "attributes": {
                    "error": {"kind": "http_exception"},
                }
            }
        }

    Accepts either:
      - A wrapper dict with "data" or "logs" key containing a list of events
      - A plain list of event dicts

    Output per entry:
        {timestamp, severity, error_type, message, trace_id, service}

    Handles None/missing fields gracefully, defaulting to empty strings.
    """
    if not raw:
        return []

    # Accept either a wrapper dict (with "data" or "logs" key) or a plain list
    if isinstance(raw, list):
        events = raw
    elif isinstance(raw, dict):
        events = raw.get("data", raw.get("logs", []))
    else:
        return []

    if not isinstance(events, list):
        return []

    normalized: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue

        attrs = event.get("attributes", event)
        # Parse tags array for service and trace_id (e.g. ["service:x", "trace_id:y"])
        tags = attrs.get("tags", [])
        tag_map = _parse_tags(tags)

        # Nested attributes (Datadog log structure has attributes.attributes)
        inner_attrs = attrs.get("attributes", {})
        if not isinstance(inner_attrs, dict):
            inner_attrs = {}

        # error_type from inner_attrs.error.kind, or top-level fallback
        error_obj = inner_attrs.get("error", {})
        if not isinstance(error_obj, dict):
            error_obj = {}
        error_type = error_obj.get("kind", "") or attrs.get("error_type", "")

        # trace_id: prefer tags, then inner attrs, then top-level attrs
        trace_id = (
            tag_map.get("trace_id", "")
            or str(inner_attrs.get("trace_id", ""))
            or str(attrs.get("trace_id", ""))
            or str(attrs.get("dd", {}).get("trace_id", "") if isinstance(attrs.get("dd"), dict) else "")
        )

        # service: prefer tags, then top-level attrs
        service = tag_map.get("service", "") or str(attrs.get("service", ""))

        normalized.append({
            "timestamp": str(attrs.get("timestamp", "")),
            "severity": _map_severity(attrs.get("status", attrs.get("severity", ""))),
            "error_type": str(error_type),
            "message": str(attrs.get("message", "")),
            "trace_id": str(trace_id),
            "service": str(service),
        })

    return normalized


def _parse_tags(tags: Any) -> dict[str, str]:
    """Parse a Datadog tags list into a key-value dict.

    Tags are formatted as "key:value" strings. Returns empty dict
    for non-list inputs or malformed entries.
    """
    if not isinstance(tags, list):
        return {}
    result: dict[str, str] = {}
    for tag in tags:
        if not isinstance(tag, str):
            continue
        parts = tag.split(":", 1)
        if len(parts) == 2:
            result[parts[0]] = parts[1]
    return result


def _map_severity(raw_status: str) -> str:
    """Normalize Datadog log status to standard severity labels."""
    mapping = {
        "error": "ERROR",
        "err": "ERROR",
        "warn": "WARN",
        "warning": "WARN",
        "info": "INFO",
        "debug": "DEBUG",
        "ok": "INFO",
    }
    return mapping.get(str(raw_status).lower(), str(raw_status).upper() or "INFO")


def _normalize_traces(raw: dict[str, Any] | list | None) -> list[dict[str, Any]]:
    """Map raw MCP trace dicts to a consistent normalized shape.

    Accepts either:
      - A wrapper dict with "data", "traces", or "spans" key containing a list
      - A plain list of span dicts

    Expected output per entry:
        {trace_id, span_id, service, resource, error, duration, start,
         error_type, error_message}

    Handles None/missing fields gracefully, defaulting to empty strings
    or reasonable defaults (0.0 for duration, False for error).
    """
    if not raw:
        return []

    # Accept either a wrapper dict or a plain list
    if isinstance(raw, list):
        spans = raw
    elif isinstance(raw, dict):
        spans = raw.get("data", raw.get("traces", raw.get("spans", [])))
    else:
        return []

    if not isinstance(spans, list):
        return []

    normalized: list[dict[str, Any]] = []
    for span in spans:
        if not isinstance(span, dict):
            continue

        attrs = span.get("attributes", span)
        meta = attrs.get("meta", {})
        error_meta = meta.get("error", {}) if isinstance(meta, dict) else {}

        normalized.append({
            "trace_id": str(attrs.get("trace_id", "")),
            "span_id": str(attrs.get("span_id", "")),
            "service": str(attrs.get("service", "")),
            "resource": str(attrs.get("resource", attrs.get("operation", attrs.get("name", "")))),
            "error": bool(attrs.get("error", False)),
            "duration": _safe_duration_ms(attrs.get("duration", 0)),
            "start": str(attrs.get("start", "")),
            "error_type": str(
                attrs.get("error_type", error_meta.get("type", ""))
            ),
            "error_message": str(
                attrs.get("error_message", error_meta.get("message", error_meta.get("msg", "")))
            ),
        })

    return normalized


def _safe_duration_ms(raw_duration: Any) -> float:
    """Convert a raw duration value to milliseconds.

    Datadog APM reports duration in nanoseconds; convert to ms.
    Falls back to 0.0 if the value is not numeric.
    """
    try:
        ns = float(raw_duration)
        return ns / 1_000_000
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# MCP result parsing
# ---------------------------------------------------------------------------


def _extract_tool_result(result: Any) -> dict | list:
    """Extract parsed data from an MCP CallToolResult.

    MCP's ``call_tool`` returns a ``CallToolResult`` object with a ``content``
    list. Each content block may be a ``TextContent`` with a ``text`` attribute
    containing JSON. This helper iterates the blocks and returns the first
    successfully parsed JSON value.

    Returns an empty dict if no parseable content is found.
    """
    if hasattr(result, "content") and result.content:
        for block in result.content:
            if hasattr(block, "text"):
                try:
                    return json.loads(block.text)
                except (json.JSONDecodeError, TypeError):
                    pass
    return {}


# ---------------------------------------------------------------------------
# Evidence fetching — public API
# ---------------------------------------------------------------------------


def fetch_evidence(
    service: str,
    window_start: datetime,
    window_end: datetime,
    pod_name: str | None = None,
    max_logs: int = 20,
    max_traces: int = 10,
) -> MCPEvidence:
    """Retrieve incident evidence via Datadog MCP server calls.

    This is a synchronous wrapper around the async MCP client.
    FastAPI runs sync route handlers in a thread pool (no running event loop),
    so ``asyncio.run()`` is safe here.

    Args:
        service: Datadog service name to filter on.
        window_start: Start of the evidence time window.
        window_end: End of the evidence time window.
        pod_name: Optional Kubernetes pod name for finer filtering.
        max_logs: Maximum number of log events to retrieve (default 20).
        max_traces: Maximum number of trace spans to retrieve (default 10).

    Returns:
        MCPEvidence with normalized logs, traces, monitors, and incidents.

    Raises:
        MCPUnavailableError: When the MCP server cannot be reached.
        MCPQueryError:       When a specific MCP tool call fails.
    """
    return asyncio.run(
        _fetch_evidence_async(
            service, window_start, window_end, pod_name, max_logs, max_traces
        )
    )


async def _fetch_evidence_async(
    service: str,
    window_start: datetime,
    window_end: datetime,
    pod_name: str | None = None,
    max_logs: int = 20,
    max_traces: int = 10,
) -> MCPEvidence:
    """Async implementation that performs the actual MCP tool calls.

    Calls four Datadog MCP tools in sequence within a single session:
      1. logs_list_events — error/warn logs filtered by service and time
      2. apm_list_traces — error traces filtered by service and time
      3. monitors_list_monitors — monitors in Alert or Warn state
      4. incidents_list_incidents — currently active incidents
    """
    async with _get_mcp_client() as session:
        from_ts = int(window_start.timestamp())
        to_ts = int(window_end.timestamp())
        tag_filter = f"service:{service}"

        # 1. Logs: errors and warnings
        log_query = f"status:(error OR warn) {tag_filter}"
        if pod_name:
            log_query += f" pod_name:{pod_name}"
        try:
            raw_logs = await session.call_tool(
                "logs_list_events",
                arguments={
                    "filter": {"query": log_query, "from": from_ts, "to": to_ts},
                    "page": {"limit": max_logs},
                },
            )
        except Exception as exc:
            raise MCPQueryError(f"logs_list_events failed: {exc}") from exc

        # 2. APM traces with errors
        try:
            raw_traces = await session.call_tool(
                "apm_list_traces",
                arguments={
                    "filter": {
                        "query": f"service:{service} error:true",
                        "from": from_ts,
                        "to": to_ts,
                    },
                    "page": {"limit": max_traces},
                },
            )
        except Exception as exc:
            raise MCPQueryError(f"apm_list_traces failed: {exc}") from exc

        # 3. Monitors in alert/warn state
        try:
            raw_monitors = await session.call_tool(
                "monitors_list_monitors",
                arguments={
                    "query": f"scope:{service}",
                    "monitor_states": "Alert,Warn",
                },
            )
        except Exception as exc:
            raise MCPQueryError(f"monitors_list_monitors failed: {exc}") from exc

        # 4. Open incidents
        try:
            raw_incidents = await session.call_tool(
                "incidents_list_incidents",
                arguments={"filter": "state:active"},
            )
        except Exception as exc:
            raise MCPQueryError(f"incidents_list_incidents failed: {exc}") from exc

        # Parse results from MCP tool call responses
        logs_data = _extract_tool_result(raw_logs)
        traces_data = _extract_tool_result(raw_traces)
        monitors_data = _extract_tool_result(raw_monitors)
        incidents_data = _extract_tool_result(raw_incidents)

        return MCPEvidence(
            logs=_normalize_logs(logs_data),
            traces=_normalize_traces(traces_data),
            monitors=(
                monitors_data.get("monitors", monitors_data.get("data", []))
                if isinstance(monitors_data, dict)
                else []
            ),
            incidents=(
                incidents_data.get("incidents", incidents_data.get("data", []))
                if isinstance(incidents_data, dict)
                else []
            ),
        )
