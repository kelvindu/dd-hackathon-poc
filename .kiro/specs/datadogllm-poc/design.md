# Design Document — datadogllm-poc

## Overview

This document describes the technical design for migrating the CloudWatch + Bedrock RCA PoC to use Datadog as the sole observability backend and the Datadog MCP server as the evidence source for the analyzer.

The existing system ships telemetry to CloudWatch and retrieves it via `cloudwatch.py` before invoking Bedrock. The target system:

1. Ships all telemetry (logs, metrics, traces) to Datadog via the Datadog Agent / OTEL collector.
2. Retrieves incident evidence via the Datadog MCP server (replacing `cloudwatch.py`).
3. Returns a richer RCA response schema (`triage_result`, `root_cause`, `evidence`, `recommended_focus`, `confidence`).
4. Instruments the Bedrock call with Datadog LLM Observability in agent mode (not agentless).
5. Removes all CloudWatch IAM bindings, manifests, and code references.

No behavioural changes to the faulty-workload service are required beyond wiring its existing structured JSON output and Prometheus metrics to the Datadog Agent.

---

## Architecture

### Component Diagram

```mermaid
graph TD
    subgraph "Kubernetes Cluster (EKS)"
        FW[faulty-workload\nFastAPI :8080]
        AN[analyzer\nFastAPI :8000]
        DD[datadog-agent\nDaemonSet]
        OC[otel-collector\nDaemonSet - optional]
    end

    subgraph "External"
        DDBE[Datadog Backend\nlogs · metrics · APM · LLM Obs]
        MCP[Datadog MCP Server\n(npx @datadog/mcp)]
        BR[AWS Bedrock\nClaude 3 Haiku]
    end

    FW -- "stdout JSON logs\n/metrics Prometheus" --> DD
    DD -- "logs + metrics + APM traces\nHTTPS" --> DDBE
    OC -- "OTLP → Datadog exporter\n(fallback path)" --> DDBE

    AN -- "MCP stdio / HTTP" --> MCP
    MCP -- "Datadog API" --> DDBE
    AN -- "InvokeModel" --> BR
    AN -- "ddtrace spans" --> DD
    DD -- "LLM Observability" --> DDBE
```

### Data Flow

```
Request → faulty-workload
         │
         ├─► structured JSON log  → stdout → Datadog Agent → Datadog Logs
         ├─► Prometheus /metrics  → Datadog Agent scrape → Datadog Metrics
         └─► (ddtrace APM)        → Datadog Agent → Datadog APM

POST /rca/analyze → analyzer
         │
         ├─1─► datadog_mcp.py  → MCP tool calls → Datadog MCP server → Datadog API
         │         logs / traces / monitors / incidents / dashboards
         │
         ├─2─► bundle.py       → compact incident bundle
         │
         ├─3─► bedrock.py      → Bedrock InvokeModel (Claude)
         │         ddtrace LLM span → Datadog Agent → Datadog LLM Obs
         │
         └─4─► AnalyzeResponse { triage_result, root_cause, evidence,
                                  recommended_focus, confidence }
```

---

## Components and Interfaces

### 1. Faulty Workload (unchanged code, new Agent wiring)

No Python code changes required. The workload already emits:
- Structured JSON to stdout (picked up by Datadog Agent log collection)
- Prometheus metrics at `/metrics` (scraped by Datadog Agent)
- Trace context headers (`X-Trace-ID`, `X-Request-ID`)

The `FAULT_SAMPLE_RATE` env var already controls log volume. Set it to `0.1` (default) to stay within cost limits.

### 2. `datadog_mcp.py` (new — replaces `cloudwatch.py`)

Provides a single public function `fetch_evidence(...)` that issues MCP tool calls to the Datadog MCP server and returns an `MCPEvidence` dict. All network I/O is isolated here; `app.py` and `bundle.py` consume only the structured output.

**MCP tools used:**

| Purpose | MCP tool |
|---|---|
| Error/warning logs | `logs_list_events` (filter: `status:error OR status:warn`, time window, service tag) |
| APM traces | `apm_list_traces` (filter: service, time window, error spans) |
| Active monitors in alert | `monitors_list_monitors` (filter: scope matches service) |
| Open incidents | `incidents_list_incidents` |
| Dashboard snapshot | `dashboards_list_dashboards` (optional, for context) |

**Public interface:**

```python
# analyzer/datadog_mcp.py

from typing import Any
from dataclasses import dataclass, field

@dataclass
class MCPEvidence:
    logs: list[dict[str, Any]] = field(default_factory=list)
    traces: list[dict[str, Any]] = field(default_factory=list)
    monitors: list[dict[str, Any]] = field(default_factory=list)
    incidents: list[dict[str, Any]] = field(default_factory=list)
    # Optional — included when dashboards are available
    dashboards: list[dict[str, Any]] = field(default_factory=list)


def fetch_evidence(
    service: str,
    window_start: datetime,
    window_end: datetime,
    pod_name: str | None = None,
    max_logs: int = 20,
    max_traces: int = 10,
) -> MCPEvidence:
    """
    Retrieve incident evidence via Datadog MCP server calls.

    Raises:
        MCPUnavailableError: When the MCP server cannot be reached.
        MCPQueryError:       When a specific MCP tool call fails.
    """
    ...
```

**MCP client configuration:**

The Datadog MCP server is started as a subprocess sidecar (stdio transport) or reached via HTTP if a pre-started server URL is provided:

```
DD_MCP_TRANSPORT=stdio   # default: spawn npx @datadog/mcp
DD_MCP_URL=http://...    # alternative: pre-started HTTP server
DD_API_KEY=<secret>
DD_APP_KEY=<secret>
DD_SITE=datadoghq.com
```

For the PoC, stdio transport is simplest: the analyzer spawns the MCP server process at startup and communicates over stdin/stdout using the MCP protocol (`mcp` Python client library).

**Pseudocode:**

```python
def _get_mcp_client() -> MCPClient:
    transport = os.environ.get("DD_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        return StdioMCPClient(
            command=["npx", "-y", "@datadog/mcp"],
            env={
                "DD_API_KEY": os.environ["DD_API_KEY"],
                "DD_APP_KEY": os.environ["DD_APP_KEY"],
                "DD_SITE": os.environ.get("DD_SITE", "datadoghq.com"),
            },
        )
    else:
        return HttpMCPClient(url=os.environ["DD_MCP_URL"])


def fetch_evidence(service, window_start, window_end, pod_name=None,
                   max_logs=20, max_traces=10) -> MCPEvidence:
    client = _get_mcp_client()

    # Build time range filter
    from_ts = int(window_start.timestamp())
    to_ts = int(window_end.timestamp())
    tag_filter = f"service:{service}"

    # 1. Logs: errors and warnings
    log_query = f"status:(error OR warn) {tag_filter}"
    if pod_name:
        log_query += f" pod_name:{pod_name}"
    raw_logs = client.call(
        "logs_list_events",
        {"filter": {"query": log_query, "from": from_ts, "to": to_ts},
         "page": {"limit": max_logs}}
    )

    # 2. APM traces with errors
    raw_traces = client.call(
        "apm_list_traces",
        {"filter": {"query": f"service:{service} error:true",
                    "from": from_ts, "to": to_ts},
         "page": {"limit": max_traces}}
    )

    # 3. Monitors in alert / warn state
    raw_monitors = client.call(
        "monitors_list_monitors",
        {"query": f"scope:{service}", "monitor_states": "Alert,Warn"}
    )

    # 4. Open incidents
    raw_incidents = client.call(
        "incidents_list_incidents",
        {"filter": "state:active"}
    )

    return MCPEvidence(
        logs=_normalize_logs(raw_logs),
        traces=_normalize_traces(raw_traces),
        monitors=raw_monitors.get("monitors", []),
        incidents=raw_incidents.get("incidents", []),
    )
```

### 3. `bundle.py` (updated)

The existing `build_bundle(logs, metrics, traces)` signature is extended to accept `MCPEvidence`:

```python
def build_bundle(evidence: MCPEvidence) -> dict:
    """
    Build a compact incident bundle from MCPEvidence.

    Returns a dict with:
      log_summary     – top 10 most severe log entries
      trace_anomalies – up to 5 error traces
      monitor_alerts  – up to 5 monitors in alert/warn
      incidents       – up to 3 active incidents
    """
    ...
```

The existing severity-sort logic is preserved. `metric_deltas` is removed (metrics are represented via monitor alerts instead, since Datadog monitors already encode metric threshold breaches).

### 4. `bedrock.py` (updated prompt and response schema)

**Response schema change:**

```python
# Old
_REQUIRED_KEYS = {"root_cause", "evidence", "impact", "recommended_fix", "confidence"}

# New
_REQUIRED_KEYS = {"triage_result", "root_cause", "evidence", "recommended_focus", "confidence"}
```

**Updated prompt:**

```python
def build_prompt(bundle: dict) -> str:
    incident_data = json.dumps(bundle, separators=(",", ":"))
    prompt = (
        "You are an SRE triage assistant. Analyse the following Kubernetes incident data. "
        "Return ONLY a JSON object with exactly these keys:\n"
        "  triage_result   – one of: noise | watch | needs-attention\n"
        "  root_cause      – most likely cause based on evidence only (string)\n"
        "  evidence        – key signals observed (string)\n"
        "  recommended_focus – operational investigation area, NOT code fixes (string)\n"
        "  confidence      – float 0.0–1.0\n"
        "Rules:\n"
        "- If evidence is weak, set confidence < 0.5 and prefix root_cause with '[TENTATIVE]'.\n"
        "- If error codes indicate a severe condition, escalate triage_result to at least 'watch'.\n"
        "- Do NOT include code-fix instructions in recommended_focus.\n"
        "- State uncertainty explicitly when evidence is insufficient.\n"
        "No explanation outside the JSON.\n\n"
        f"Incident data: {incident_data}"
    )
    return prompt
```

**LLM Observability wiring change (agent mode):**

```python
# Old (agentless — sends directly to Datadog API)
LLMObs.enable(agentless_enabled=True, api_key=os.environ["DD_API_KEY"], ...)

# New (agent mode — sends to local Datadog Agent)
LLMObs.enable(
    ml_app=os.environ.get("DD_LLMOBS_ML_APP", "datadogllm-poc"),
    agentless_enabled=False,  # route via Datadog Agent
)
# No api_key needed here; agent picks it up from its own config
```

The `_init_llmobs()` function catches all exceptions and logs a warning on failure without re-raising, preserving graceful degradation.

### 5. `app.py` (updated orchestration)

```python
# analyzer/app.py  (key changes only)

import datadog_mcp  # replaces cloudwatch
from datadog_mcp import MCPUnavailableError, MCPQueryError

class AnalyzeResponse(BaseModel):
    triage_result: str          # noise | watch | needs-attention
    root_cause: str
    evidence: str
    recommended_focus: str      # replaces recommended_fix
    confidence: float

@app.post("/rca/analyze", response_model=AnalyzeResponse)
def rca_analyze(body: AnalyzeRequest) -> AnalyzeResponse:
    # 1. Fetch evidence via Datadog MCP
    try:
        evidence = datadog_mcp.fetch_evidence(
            service=body.service,
            window_start=body.window_start,
            window_end=body.window_end,
            pod_name=body.pod_name,
        )
    except (MCPUnavailableError, MCPQueryError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # 2. Guard: no evidence → 404
    if not evidence.logs and not evidence.monitors and not evidence.incidents:
        raise HTTPException(
            status_code=404,
            detail="No Datadog evidence found for the specified window",
        )

    # 3. Build compact bundle
    incident_bundle = bundle.build_bundle(evidence)

    # 4. Invoke Bedrock
    try:
        rca = bedrock.invoke_rca(incident_bundle)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f"Bedrock non-JSON: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # 5. Return
    return AnalyzeResponse(
        triage_result=rca["triage_result"],
        root_cause=rca["root_cause"],
        evidence=rca["evidence"],
        recommended_focus=rca["recommended_focus"],
        confidence=float(rca["confidence"]),
    )
```

---

## Data Models

### `MCPEvidence`

```python
@dataclass
class MCPEvidence:
    logs: list[dict]       # Normalized log entries from Datadog MCP logs_list_events
    traces: list[dict]     # Normalized trace spans from apm_list_traces
    monitors: list[dict]   # Monitor objects from monitors_list_monitors
    incidents: list[dict]  # Incident objects from incidents_list_incidents
    dashboards: list[dict] # Optional dashboard entries
```

Normalized log entry shape (mirrors existing cloudwatch.py output for bundle.py compatibility):

```python
{
    "timestamp": str,    # ISO-8601
    "severity":  str,    # ERROR | WARN | INFO
    "error_type": str,   # e.g. "http_exception"
    "message":   str,
    "trace_id":  str,
    "service":   str,
}
```

### `IncidentBundle`

```python
{
    "log_summary":     list[dict],   # ≤ 10 entries, severity-sorted
    "trace_anomalies": list[dict],   # ≤ 5 error traces
    "monitor_alerts":  list[dict],   # ≤ 5 monitors in Alert/Warn
    "incidents":       list[dict],   # ≤ 3 active incidents
}
```

### `AnalyzeRequest` (unchanged fields)

```python
class AnalyzeRequest(BaseModel):
    incident_id:  str
    service:      str
    window_start: datetime
    window_end:   datetime
    namespace:    Optional[str] = None
    pod_name:     Optional[str] = None
```

### `AnalyzeResponse` (updated)

```python
class AnalyzeResponse(BaseModel):
    triage_result:     str    # "noise" | "watch" | "needs-attention"
    root_cause:        str
    evidence:          str
    recommended_focus: str    # operational focus area; no code fixes
    confidence:        float  # 0.0 – 1.0
```

### Error types (exceptions in `datadog_mcp.py`)

```python
class MCPUnavailableError(RuntimeError):
    """Raised when the MCP server process cannot be started or reached."""

class MCPQueryError(RuntimeError):
    """Raised when an individual MCP tool call fails."""
```

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Structured log fields are always complete

*For any* log record emitted by `JsonFormatter`, the resulting JSON string must contain all seven required fields: `timestamp`, `service`, `severity`, `request_id`, `error_type`, `message`, and `trace_id`.

**Validates: Requirements 1.2**

---

### Property 2: Trace ID context propagation

*For any* arbitrary `trace_id` value set in the request context variable, all log records emitted within that context must include that exact `trace_id` in the JSON output.

**Validates: Requirements 1.3**

---

### Property 3: Triage result is a valid enum value

*For any* RCA response dict returned by `invoke_rca`, the `triage_result` field must be exactly one of `"noise"`, `"watch"`, or `"needs-attention"`.

**Validates: Requirements 4.1**

---

### Property 4: Severe error codes prevent noise classification

*For any* incident bundle where the evidence is weak (few log entries, no active monitors) but the log entries contain an `error_type` classified as severe (e.g., `http_exception`, `dependency_timeout`), the `triage_result` returned must be `"watch"` or `"needs-attention"`, never `"noise"`.

**Validates: Requirements 4.2, 4.3**

---

### Property 5: Required RCA output fields are always present

*For any* valid `invoke_rca` result dict, all five fields — `triage_result`, `root_cause`, `evidence`, `recommended_focus`, and `confidence` — must be present and non-`None`.

**Validates: Requirements 5.3, 7.4**

---

### Property 6: Request schema accepts all valid input combinations

*For any* valid combination of required fields (`incident_id`, `service`, `window_start`, `window_end`) with or without optional fields (`namespace`, `pod_name`), `POST /rca/analyze` must not return HTTP 422.

**Validates: Requirements 7.2**

---

### Property 7: Bundle size is bounded regardless of evidence volume

*For any* `MCPEvidence` object with arbitrarily many log entries, `build_bundle` must return a `log_summary` containing at most 10 entries, `trace_anomalies` at most 5, `monitor_alerts` at most 5, and `incidents` at most 3.

**Validates: Requirements 7.3, 9.2, 9.3**

---

### Property 8: Prompt token budget is respected

*For any* incident bundle, `build_prompt` must return a string whose character length is at most 8 000 characters (≈ 2 000 tokens), the hard ceiling for the Haiku context budget used in the PoC.

**Validates: Requirements 9.2**

---

### Property 9: Datadog initialization failure does not crash the process

*For any* exception raised during `LLMObs.enable()` (e.g., `KeyError` for missing `DD_API_KEY`, `ConnectionError`, `ImportError`), the `_init_llmobs()` function must catch the exception, emit a warning log, and return without re-raising.

**Validates: Requirements 8.3, 9.5**

---

## Error Handling

### MCP Server Unavailable

When `fetch_evidence` cannot reach the MCP server (process fails to start, connection refused, timeout):
- Raise `MCPUnavailableError`.
- `app.py` catches this and returns HTTP 502 with the error detail.
- No fallback to CloudWatch or any other source — the system fails clearly rather than silently degrading to a different data source.

### Individual MCP Tool Call Failure

When a specific tool call (e.g., `logs_list_events`) fails:
- Raise `MCPQueryError` with the tool name and error detail.
- This propagates as HTTP 502 from `app.py`.
- Partial evidence (e.g., logs succeeded but traces failed) is **not** used; the full evidence set is required for a reliable triage.

### No Evidence Found

When MCP calls succeed but return empty results for the given window/service:
- `app.py` returns HTTP 404 with `"No Datadog evidence found for the specified window"`.

### Bedrock Failure

Unchanged from current behavior:
- `json.JSONDecodeError` → HTTP 502
- `ValueError` (missing keys) → HTTP 502
- Both are annotated on the LLM Obs span before re-raising.

### Datadog Agent / LLM Observability Unavailable

`_init_llmobs()` catches all exceptions and logs a warning. If the span cannot be opened during `invoke_rca`, the span variable is `None` and all span-related branches are skipped. The Bedrock call proceeds normally.

### Graceful Startup Without Datadog

If `DD_LLMOBS_ENABLED` is not set or is `false`, the entire LLM Obs code path is bypassed at module level. The system starts and operates without any Datadog dependency at the Python layer.

---

## Local Deployment (Docker Compose)

Local testing runs the full stack on a laptop before any AWS or Kubernetes involvement. The only external services required are Datadog (SaaS — any account works) and AWS Bedrock (for the RCA call). No EKS, no CloudWatch, no OTEL collector.

### Services in `docker-compose.yml`

```
faulty-workload   :8080   generates controlled faults; logs to stdout
datadog-agent     :8126   collects logs from containers, scrapes /metrics, forwards to Datadog
datadog-mcp       :3000   Datadog MCP server (HTTP transport for local use)
analyzer          :8000   FastAPI RCA service; calls datadog-mcp and Bedrock
```

### Updated `docker-compose.yml`

```yaml
services:

  faulty-workload:
    build:
      context: ./faulty-workload
    env_file: .env
    ports:
      - "8080:8080"
    environment:
      SERVICE_NAME: faulty-workload
      FAULT_SAMPLE_RATE: "1.0"        # log every request locally
      # ddtrace APM → local Datadog Agent
      DD_AGENT_HOST: datadog-agent
      DD_TRACE_ENABLED: "true"
      DD_SERVICE: faulty-workload
      DD_ENV: local
      DD_VERSION: "0.1.0"
    labels:
      # Datadog Agent autodiscovery: collect logs from this container
      com.datadoghq.ad.logs: '[{"source":"python","service":"faulty-workload"}]'
    depends_on:
      - datadog-agent

  datadog-agent:
    image: gcr.io/datadoghq/agent:7
    env_file: .env                   # picks up DD_API_KEY, DD_SITE
    environment:
      DD_LOGS_ENABLED: "true"
      DD_LOGS_CONFIG_CONTAINER_COLLECT_ALL: "true"
      DD_CONTAINER_EXCLUDE: "name:datadog-agent"
      DD_APM_ENABLED: "true"
      DD_APM_NON_LOCAL_TRAFFIC: "true"
      DD_PROMETHEUS_SCRAPE_ENABLED: "true"
      DD_PROMETHEUS_SCRAPE_CHECKS: >-
        [{"autodiscovery": {"kubernetes_container_names": []},
          "configurations": [{"url": "http://faulty-workload:8080/metrics",
                              "namespace": "faulty_workload",
                              "metrics": ["request_count_total","error_count_total",
                                          "warning_count_total","timeout_count_total",
                                          "latency_ms_bucket","latency_ms_count"]}]}]
      DD_DOGSTATSD_NON_LOCAL_TRAFFIC: "true"
    ports:
      - "8126:8126"   # APM trace intake
      - "8125:8125/udp"  # DogStatsD
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /proc:/host/proc:ro
      - /sys/fs/cgroup:/host/sys/fs/cgroup:ro

  datadog-mcp:
    image: node:20-slim
    working_dir: /app
    command: >
      sh -c "npm install -g @datadog/mcp@latest &&
             npx @datadog/mcp --transport http --port 3000"
    env_file: .env                   # picks up DD_API_KEY, DD_APP_KEY, DD_SITE
    ports:
      - "3000:3000"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000/health"]
      interval: 10s
      timeout: 5s
      retries: 5

  analyzer:
    build:
      context: ./analyzer
    env_file: .env
    ports:
      - "8000:8000"
    environment:
      SERVICE_NAME: analyzer
      # Datadog MCP — HTTP transport pointing to the local mcp container
      DD_MCP_TRANSPORT: http
      DD_MCP_URL: http://datadog-mcp:3000
      # ddtrace APM + LLM Observability via local Agent
      DD_AGENT_HOST: datadog-agent
      DD_TRACE_ENABLED: "true"
      DD_SERVICE: analyzer
      DD_ENV: local
      DD_VERSION: "0.1.0"
      DD_LLMOBS_ENABLED: "true"
      DD_LLMOBS_AGENTLESS_ENABLED: "false"  # agent mode
      DD_LLMOBS_ML_APP: datadogllm-poc
      # AWS credentials for Bedrock
      AWS_REGION: ${AWS_REGION:-us-east-1}
      AWS_ACCESS_KEY_ID: ${AWS_ACCESS_KEY_ID:-}
      AWS_SECRET_ACCESS_KEY: ${AWS_SECRET_ACCESS_KEY:-}
      AWS_SESSION_TOKEN: ${AWS_SESSION_TOKEN:-}
    volumes:
      - "${HOME}/.aws:/root/.aws:ro"
    depends_on:
      datadog-agent:
        condition: service_started
      datadog-mcp:
        condition: service_healthy
      faulty-workload:
        condition: service_started
```

### Updated `.env.example`

```dotenv
# AWS — Bedrock only (no CloudWatch needed)
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_SESSION_TOKEN=

BEDROCK_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0
BEDROCK_MAX_TOKENS=512

# Datadog — required for local testing
DD_API_KEY=              # Datadog API key (Agent + LLM Obs)
DD_APP_KEY=              # Datadog Application key (MCP server queries)
DD_SITE=datadoghq.com

# Datadog LLM Observability
DD_LLMOBS_ENABLED=true
DD_LLMOBS_ML_APP=datadogllm-poc
DD_LLMOBS_AGENTLESS_ENABLED=false

# Faulty workload fault tuning
FAULT_SAMPLE_RATE=1.0
HTTP_500_PROBABILITY=0.05
LATENCY_SPIKE_PROBABILITY=0.05
LATENCY_SPIKE_MIN_S=2.0
LATENCY_SPIKE_MAX_S=5.0
TIMEOUT_EVERY_N=50
MEMORY_PRESSURE_THRESHOLD=100

# Local smoke test
WORKLOAD_URL=http://localhost:8080
ANALYZER_URL=http://localhost:8000
SMOKE_REQUEST_COUNT=50
SMOKE_INGESTION_WAIT_SECS=90   # Datadog ingestion is slightly slower than CloudWatch
```

### Local Quickstart

```bash
# Prerequisites: Docker, AWS credentials with Bedrock access, Datadog account

# 1. Create .env from the example and fill in DD_API_KEY, DD_APP_KEY, AWS creds
cp .env.example .env
$EDITOR .env

# 2. Build and start all services
docker compose up --build

# 3. Verify services are healthy
curl http://localhost:8080/          # → {"status":"ok"}
curl http://localhost:8000/health    # → {"status":"ok"}
curl http://localhost:3000/health    # → MCP server OK

# 4. Generate some fault traffic (50 requests)
for i in $(seq 1 50); do curl -s http://localhost:8080/ > /dev/null; done

# 5. Wait for Datadog ingestion (~90s), then run the smoke test
sleep 90
./scripts/smoke_test.sh

# 6. Open the RCA UI
open http://localhost:8000
```

### Local Smoke Test (`scripts/smoke_test.sh`)

The existing smoke test script is updated to:
- Remove the 60-second CloudWatch ingestion note; replace with 90-second Datadog ingestion wait
- Update the expected response fields: `triage_result`, `recommended_focus` instead of `impact`, `recommended_fix`
- Assert `triage_result` is one of `noise`, `watch`, `needs-attention`

```bash
#!/usr/bin/env bash
set -euo pipefail

WORKLOAD_URL="${WORKLOAD_URL:-http://localhost:8080}"
ANALYZER_URL="${ANALYZER_URL:-http://localhost:8000}"
REQUEST_COUNT="${SMOKE_REQUEST_COUNT:-50}"
WAIT_SECS="${SMOKE_INGESTION_WAIT_SECS:-90}"

echo "→ Sending $REQUEST_COUNT requests to faulty-workload..."
for i in $(seq 1 "$REQUEST_COUNT"); do
  curl -s "$WORKLOAD_URL/" > /dev/null
done

echo "→ Waiting ${WAIT_SECS}s for Datadog ingestion..."
sleep "$WAIT_SECS"

WINDOW_END=$(date -u +"%Y-%m-%dT%H:%M:%S")
WINDOW_START=$(date -u -v-5M +"%Y-%m-%dT%H:%M:%S" 2>/dev/null || \
               date -u -d '5 minutes ago' +"%Y-%m-%dT%H:%M:%S")

echo "→ Calling POST /rca/analyze ..."
RESPONSE=$(curl -sf -X POST "$ANALYZER_URL/rca/analyze" \
  -H "Content-Type: application/json" \
  -d "{
    \"incident_id\": \"smoke-test-001\",
    \"service\": \"faulty-workload\",
    \"window_start\": \"$WINDOW_START\",
    \"window_end\": \"$WINDOW_END\"
  }")

echo "$RESPONSE" | jq .

# Assertions
TRIAGE=$(echo "$RESPONSE" | jq -r '.triage_result')
CONFIDENCE=$(echo "$RESPONSE" | jq -r '.confidence')

if [[ "$TRIAGE" != "noise" && "$TRIAGE" != "watch" && "$TRIAGE" != "needs-attention" ]]; then
  echo "FAIL: triage_result='$TRIAGE' is not a valid value" >&2; exit 1
fi

if [[ -z "$(echo "$RESPONSE" | jq -r '.root_cause')" ]]; then
  echo "FAIL: root_cause is empty" >&2; exit 1
fi

if [[ -z "$(echo "$RESPONSE" | jq -r '.recommended_focus')" ]]; then
  echo "FAIL: recommended_focus is empty" >&2; exit 1
fi

echo "✓ triage_result=$TRIAGE  confidence=$CONFIDENCE"
echo "✓ Smoke test passed"
```

### MCP Transport: Local vs Cloud

| Environment | `DD_MCP_TRANSPORT` | MCP server location |
|---|---|---|
| Local Docker Compose | `http` | `datadog-mcp` container on port 3000 |
| Kubernetes (EKS) | `http` | Dedicated `datadog-mcp` Kubernetes Deployment + Service |
| Local dev (no Docker) | `stdio` | `npx @datadog/mcp` spawned as subprocess by analyzer |

The `stdio` transport is convenient for bare-metal local development (running `uvicorn` directly). The `http` transport is used everywhere Docker or Kubernetes is involved, since spawning `npx` inside a Python container is fragile.

### Local Development Without Docker

For running services directly with `uvicorn` (fastest iteration cycle):

```bash
# Terminal 1 — faulty-workload
cd faulty-workload
pip install -r requirements.txt
DD_AGENT_HOST=localhost DD_TRACE_ENABLED=true \
  uvicorn app:app --host 0.0.0.0 --port 8080 --reload

# Terminal 2 — Datadog MCP server (stdio or HTTP)
npx -y @datadog/mcp --transport http --port 3000
# Requires DD_API_KEY and DD_APP_KEY in environment

# Terminal 3 — analyzer
cd analyzer
pip install -r requirements.txt
DD_MCP_TRANSPORT=http DD_MCP_URL=http://localhost:3000 \
DD_LLMOBS_ENABLED=true DD_LLMOBS_AGENTLESS_ENABLED=true \
  uvicorn app:app --host 0.0.0.0 --port 8000 --reload
# Use agentless_enabled=true here since no local Datadog Agent is running
# This sends LLM spans directly to Datadog API (requires DD_API_KEY)
```

### Datadog Checks After Local Startup

Once the stack is running, verify each signal reaches Datadog before testing on AWS:

1. **Logs** — [Log Explorer](https://app.datadoghq.com/logs): filter `service:faulty-workload env:local` — should see ERROR/WARNING JSON entries within 60–90s
2. **Metrics** — [Metrics Explorer](https://app.datadoghq.com/metric/explorer): query `faulty_workload.request_count_total` — should graph after first scrape (~30s)
3. **APM** — [APM Services](https://app.datadoghq.com/apm/services): `faulty-workload` and `analyzer` should appear after first traced request
4. **LLM Observability** — [LLM Obs](https://app.datadoghq.com/llm/traces): `datadogllm-poc` app should show traces after the first `/rca/analyze` call

All four signals must be confirmed locally before porting to EKS.

---

## Testing Strategy

### Unit Tests

Focus on specific examples and edge cases that are not covered by property tests:

- `test_datadog_mcp.py`: Mock the MCP client; verify `fetch_evidence` calls the correct tools with correct parameters; verify `MCPUnavailableError` is raised when the subprocess fails to start.
- `test_bundle.py` (extend existing): Verify `build_bundle(MCPEvidence(...))` produces the correct keys; verify empty evidence produces empty sections rather than errors.
- `test_bedrock.py`: Verify `build_prompt` includes all five output key names; verify prompt contains uncertainty instruction; verify `invoke_rca` raises `ValueError` on missing required keys; mock `LLMObs` and verify span lifecycle on success and error.
- `test_app.py`: Verify `POST /rca/analyze` returns HTTP 502 on `MCPUnavailableError`; HTTP 404 on empty evidence; validates all five response fields on success.

### Property-Based Tests

Uses **Hypothesis** (Python), minimum 100 iterations per property.

Tag format: `# Feature: datadogllm-poc, Property {N}: {text}`

```python
# Property 1 — log fields completeness
@given(
    message=st.text(min_size=1),
    severity=st.sampled_from(["DEBUG", "INFO", "WARNING", "ERROR"]),
    extra=st.fixed_dictionaries({
        "error_type": st.text(),
        "trace_id": st.text(),
    })
)
@settings(max_examples=100)
def test_log_fields_complete(message, severity, extra):
    # Feature: datadogllm-poc, Property 1: structured log fields always complete
    ...

# Property 3 — triage enum
@given(rca_dict=st.fixed_dictionaries({
    "triage_result": st.sampled_from(["noise", "watch", "needs-attention"]),
    ...
}))
def test_triage_result_valid_enum(rca_dict): ...

# Property 7 — bundle size bounds
@given(evidence=st.builds(
    MCPEvidence,
    logs=st.lists(st.fixed_dictionaries({...}), min_size=0, max_size=200),
    ...
))
def test_bundle_size_bounded(evidence):
    b = build_bundle(evidence)
    assert len(b["log_summary"]) <= 10
    assert len(b["trace_anomalies"]) <= 5
    assert len(b["monitor_alerts"]) <= 5
    assert len(b["incidents"]) <= 3

# Property 8 — prompt token budget
@given(bundle=st.builds(IncidentBundle, ...))
def test_prompt_within_token_budget(bundle):
    prompt = build_prompt(bundle)
    assert len(prompt) <= 8000

# Property 9 — LLMObs init failure is non-fatal
@given(exc=st.one_of(
    st.just(KeyError("DD_API_KEY")),
    st.just(ConnectionError("timeout")),
    st.just(ImportError("ddtrace not installed")),
))
def test_llmobs_init_failure_non_fatal(exc):
    with mock.patch("ddtrace.llmobs.LLMObs.enable", side_effect=exc):
        _init_llmobs()  # must not raise
```

### Integration Tests

Run against a live Datadog account (CI/CD optional, manual for PoC):

- Verify logs emitted by faulty-workload appear in Datadog Logs within 60s.
- Verify Prometheus metrics appear as Datadog metrics within 60s.
- Verify APM traces from the analyzer appear in Datadog APM.
- Verify LLM Observability spans appear in Datadog LLM Obs UI after a `/rca/analyze` call.

### Kubernetes Deployment Validation

- Deploy with `DD_LLMOBS_ENABLED=false` → system starts, `/health` returns 200.
- Deploy with `DD_LLMOBS_ENABLED=true` and valid `DD_API_KEY` → LLM spans visible in Datadog.
- Remove Datadog Agent from cluster → workload and analyzer continue running; no crash.
