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
from mcp.client.stdio import stdio_client

# Import streamable HTTP client — available in mcp>=1.8.0
try:
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:
    streamablehttp_client = None  # type: ignore[assignment]

# Fallback to SSE for older mcp versions
try:
    from mcp.client.sse import sse_client
except ImportError:
    sse_client = None  # type: ignore[assignment]


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
      - "stdio" (default): spawns `npx -y @datadog/mcp` subprocess
      - "http": connects to DD_MCP_URL via Streamable HTTP (or SSE fallback)

    For the managed Datadog MCP endpoint (https://mcp.datadoghq.com/...),
    authentication is done via DD-API-KEY and DD-APPLICATION-KEY headers.

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
                # HTTP transport — for managed Datadog MCP or self-hosted
                dd_site = os.environ.get("DD_SITE", "datadoghq.com")
                default_url = f"https://mcp.{dd_site}/api/unstable/mcp-server/mcp"
                url = os.environ.get("DD_MCP_URL", default_url)

                # Auth headers for the managed Datadog MCP endpoint
                try:
                    api_key = os.environ["DD_API_KEY"]
                    app_key = os.environ["DD_APP_KEY"]
                except KeyError as exc:
                    raise MCPUnavailableError(
                        f"Missing required environment variable for MCP HTTP transport: {exc}"
                    ) from exc

                headers = {
                    "DD-API-KEY": api_key,
                    "DD-APPLICATION-KEY": app_key,
                }

                # Prefer streamable HTTP client, fall back to SSE
                if streamablehttp_client is not None:
                    transport_ctx = streamablehttp_client(url, headers=headers)
                    transport_result = await stack.enter_async_context(transport_ctx)
                    # streamablehttp_client may yield 2 or 3 values depending on version
                    if isinstance(transport_result, tuple) and len(transport_result) == 3:
                        read_stream, write_stream, _ = transport_result
                    elif isinstance(transport_result, tuple) and len(transport_result) == 2:
                        read_stream, write_stream = transport_result
                    else:
                        read_stream, write_stream = transport_result
                elif sse_client is not None:
                    read_stream, write_stream = await stack.enter_async_context(
                        sse_client(url, headers=headers)
                    )
                else:
                    raise MCPUnavailableError(
                        "No HTTP MCP client available. Install mcp>=1.8.0 for streamablehttp_client."
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


def _parse_tsv_response(text: str) -> list[dict[str, str]]:
    """Parse the Datadog MCP TSV response format into a list of dicts.

    Format:
        <METADATA>...xml metadata...</METADATA>
        <TSV_DATA>
        col1\\tcol2\\tcol3
        val1\\tval2\\tval3
        ...
    """
    rows: list[dict[str, str]] = []
    try:
        # Find the TSV_DATA section
        tsv_start = text.index("<TSV_DATA>") + len("<TSV_DATA>")
        tsv_text = text[tsv_start:].strip()

        lines = tsv_text.split("\n")
        if len(lines) < 2:
            return []

        # First line is the header
        headers = lines[0].split("\t")
        # Remaining lines are data rows
        for line in lines[1:]:
            if not line.strip():
                continue
            values = line.split("\t")
            row = {}
            for i, header in enumerate(headers):
                row[header.strip()] = values[i].strip() if i < len(values) else ""
            rows.append(row)
    except (ValueError, IndexError):
        pass
    return rows


def _extract_tool_result(result: Any) -> dict | list:
    """Extract parsed data from an MCP CallToolResult.

    The managed Datadog MCP server returns data in two possible formats:
    1. JSON (for some tools) — parsed directly
    2. TSV with XML metadata (for search tools) — parsed into a list of dicts

    Returns an empty dict/list if no parseable content is found.
    """
    if not hasattr(result, "content") or not result.content:
        return {}

    for block in result.content:
        if not hasattr(block, "text") or not block.text:
            continue
        text = block.text

        # Try JSON first
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass

        # Parse Datadog MCP TSV format: <METADATA>...</METADATA>\n<TSV_DATA>\nheader\nrow1\n...
        if "<TSV_DATA>" in text:
            return _parse_tsv_response(text)

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

    When MOCK_MCP=true is set, returns synthetic evidence without calling
    Datadog. Useful for local testing without Datadog credentials.

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
    # Mock mode — return synthetic evidence without calling Datadog
    if os.environ.get("MOCK_MCP", "").lower() == "true":
        return MCPEvidence(
            logs=[
                {
                    "timestamp": window_start.isoformat() + "Z",
                    "severity": "ERROR",
                    "error_type": "http_exception",
                    "message": "[MOCK] Injected random internal server error",
                    "trace_id": "mock-trace-001",
                    "service": service,
                },
                {
                    "timestamp": window_start.isoformat() + "Z",
                    "severity": "WARNING",
                    "error_type": "dependency_timeout",
                    "message": "[MOCK] Simulated dependency timeout on request #50",
                    "trace_id": "mock-trace-002",
                    "service": service,
                },
            ],
            traces=[
                {
                    "trace_id": "mock-trace-001",
                    "span_id": "mock-span-001",
                    "service": service,
                    "resource": "GET /",
                    "error": True,
                    "duration": 4500.0,
                    "start": window_start.isoformat(),
                    "error_type": "http_exception",
                    "error_message": "Internal Server Error",
                },
            ],
            monitors=[],
            incidents=[],
        )

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

    Calls Datadog MCP tools in sequence within a single session to fetch
    logs, traces, monitors, and incidents.
    """
    async with _get_mcp_client() as session:
        # Discover available tools
        tools_response = await session.list_tools()
        available_tools = {t.name for t in tools_response.tools}

        from_ts = int(window_start.timestamp())
        to_ts = int(window_end.timestamp())
        tag_filter = f"service:{service}"

        # 1. Logs: errors and warnings
        log_query = f"status:(error OR warn) {tag_filter}"
        if pod_name:
            log_query += f" pod_name:{pod_name}"

        # Use the correct tool name — check available tools
        logs_tool = next((t for t in ["search_datadog_logs", "list_logs", "logs_list_events", "analyze_datadog_logs"] if t in available_tools), None)
        traces_tool = next((t for t in ["search_datadog_spans", "list_traces", "apm_list_traces", "get_datadog_trace"] if t in available_tools), None)
        monitors_tool = next((t for t in ["search_datadog_monitors", "list_monitors", "monitors_list_monitors"] if t in available_tools), None)
        incidents_tool = next((t for t in ["search_datadog_incidents", "list_incidents", "incidents_list_incidents"] if t in available_tools), None)

        raw_logs: Any = {}
        raw_traces: Any = {}
        raw_monitors: Any = {}
        raw_incidents: Any = {}

        if logs_tool:
            try:
                raw_logs = await session.call_tool(
                    logs_tool,
                    arguments={
                        "query": log_query,
                    },
                )
            except Exception as exc:
                raise MCPQueryError(f"{logs_tool} failed: {exc}") from exc
        else:
            import logging
            logging.getLogger(__name__).warning("No logs tool found in MCP tools: %s", sorted(available_tools))

        if traces_tool:
            try:
                raw_traces = await session.call_tool(
                    traces_tool,
                    arguments={
                        "query": f"service:{service} status:error",
                        "from": str(window_start.isoformat()) + "Z",
                        "to": str(window_end.isoformat()) + "Z",
                        "limit": max_traces,
                    },
                )
            except Exception as exc:
                raise MCPQueryError(f"{traces_tool} failed: {exc}") from exc

        if monitors_tool:
            try:
                raw_monitors = await session.call_tool(
                    monitors_tool,
                    arguments={},
                )
            except Exception as exc:
                raise MCPQueryError(f"{monitors_tool} failed: {exc}") from exc

        if incidents_tool:
            try:
                raw_incidents = await session.call_tool(
                    incidents_tool,
                    arguments={},
                )
            except Exception as exc:
                raise MCPQueryError(f"{incidents_tool} failed: {exc}") from exc

        # Parse results from MCP tool call responses

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
