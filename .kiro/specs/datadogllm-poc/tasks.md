# Implementation Plan: datadogllm-poc

## Overview

Migrate the CloudWatch + Bedrock RCA PoC to use Datadog as the sole observability backend.
The full stack is validated locally via Docker Compose **before** any Kubernetes/EKS work begins.
Phases 1–4 (foundation → local smoke test → property tests) must all pass before Phase 5 (k8s) starts.

---

## Tasks

### Phase 1 — Foundation (env, deps, config)

- [x] 1. Update environment and dependency files
  - [x] 1.1 Update `.env.example`
    - Remove `LOG_GROUP`, `METRIC_NAMESPACE` CloudWatch variables
    - Add `DD_API_KEY`, `DD_APP_KEY`, `DD_SITE`, `DD_MCP_TRANSPORT`, `DD_MCP_URL`, `DD_LLMOBS_AGENTLESS_ENABLED`
    - Update `DD_LLMOBS_ML_APP` default to `datadogllm-poc`
    - Set `SMOKE_INGESTION_WAIT_SECS=90`
    - _Requirements: 2.1, 2.5_

  - [x] 1.2 Update `analyzer/requirements.txt`
    - Add `mcp` (Python MCP client library)
    - Ensure `ddtrace[openai]` is present (already at `2.9.4` — verify extras cover LLM Obs)
    - Add `hypothesis` for property tests
    - Remove any boto3 cloudwatch-only transitive deps if applicable
    - _Requirements: 3.1, 8.1_

  - [x] 1.3 Update `faulty-workload/requirements.txt`
    - Add `ddtrace` for APM tracing
    - _Requirements: 2.2, 1.3_

---

### Phase 2 — Core code changes

- [x] 2. Create `analyzer/datadog_mcp.py`
  - [x] 2.1 Define `MCPEvidence` dataclass and custom exceptions
    - Implement `MCPEvidence` dataclass with `logs`, `traces`, `monitors`, `incidents`, `dashboards` fields
    - Implement `MCPUnavailableError(RuntimeError)` and `MCPQueryError(RuntimeError)`
    - _Requirements: 3.1, 3.2_

  - [x] 2.2 Implement `_get_mcp_client()` with stdio and HTTP transport
    - Read `DD_MCP_TRANSPORT` env var (`stdio` default, `http` alternative)
    - For stdio: spawn `npx -y @datadog/mcp` subprocess with `DD_API_KEY`, `DD_APP_KEY`, `DD_SITE` env vars
    - For http: connect to `DD_MCP_URL`
    - Raise `MCPUnavailableError` when the client cannot be created/connected
    - _Requirements: 3.1_

  - [x] 2.3 Implement `_normalize_logs()` and `_normalize_traces()` helpers
    - `_normalize_logs()`: map raw MCP log event dicts to `{timestamp, severity, error_type, message, trace_id, service}`
    - `_normalize_traces()`: map raw MCP trace dicts to a consistent shape
    - _Requirements: 1.2, 3.2_

  - [x] 2.4 Implement `fetch_evidence()`
    - Call MCP tools: `logs_list_events`, `apm_list_traces`, `monitors_list_monitors`, `incidents_list_incidents`
    - Apply time-range and service/pod filters; respect `max_logs=20`, `max_traces=10`
    - Wrap tool call failures in `MCPQueryError`
    - Return `MCPEvidence` with normalized data
    - _Requirements: 3.1, 3.2, 9.1, 9.3_

  - [ ]* 2.5 Write unit tests for `datadog_mcp.py`
    - Mock MCP client; verify each tool is called with correct parameters
    - Verify `MCPUnavailableError` is raised when subprocess fails to start
    - Verify `MCPQueryError` is raised on individual tool call failure
    - _Requirements: 3.1, 3.2_

- [x] 3. Update `analyzer/bundle.py`
  - [x] 3.1 Change `build_bundle` signature to accept `MCPEvidence`
    - New signature: `build_bundle(evidence: MCPEvidence) -> dict`
    - Return dict with keys: `log_summary` (≤10), `trace_anomalies` (≤5), `monitor_alerts` (≤5), `incidents` (≤3)
    - Preserve existing severity-sort logic for `log_summary`
    - Remove `metric_deltas` key
    - _Requirements: 7.3, 9.2, 9.3_

  - [ ]* 3.2 Write property test for `build_bundle` — Property 7: Bundle size bounded
    - **Property 7: Bundle size is bounded regardless of evidence volume**
    - **Validates: Requirements 7.3, 9.2, 9.3**
    - Use `hypothesis` `@given` with arbitrarily large `MCPEvidence` (up to 200 logs, traces, monitors, incidents)
    - Assert `len(log_summary) <= 10`, `len(trace_anomalies) <= 5`, `len(monitor_alerts) <= 5`, `len(incidents) <= 3`

  - [ ]* 3.3 Write unit tests for updated `bundle.py`
    - Verify `build_bundle(MCPEvidence(...))` returns correct keys
    - Verify empty `MCPEvidence` produces empty sections without errors
    - _Requirements: 7.3_

- [x] 4. Update `analyzer/bedrock.py`
  - [x] 4.1 Update `_REQUIRED_KEYS` and `build_prompt()`
    - Replace `{"root_cause", "evidence", "impact", "recommended_fix", "confidence"}` with `{"triage_result", "root_cause", "evidence", "recommended_focus", "confidence"}`
    - Update `build_prompt()` to include triage classification rules, uncertainty/tentative logic, and `recommended_focus` instruction (no code fixes)
    - _Requirements: 4.1, 4.2, 4.3, 5.3, 6.3, 7.4_

  - [x] 4.2 Update `_init_llmobs()` to use agent mode
    - Change default `ml_app` to `datadogllm-poc`
    - Set `agentless_enabled=False` (routes via local Datadog Agent)
    - Remove `api_key` argument from `LLMObs.enable()` call (agent handles it)
    - Ensure all exceptions are caught, warning-logged, and not re-raised
    - _Requirements: 8.1, 8.3, 9.5_

  - [ ]* 4.3 Write property test for `build_prompt` — Property 8: Prompt token budget ≤ 8000 chars
    - **Property 8: Prompt token budget is respected**
    - **Validates: Requirements 9.2**
    - Use `hypothesis` `@given` with arbitrary incident bundles (varied log/trace/monitor/incident list lengths)
    - Assert `len(build_prompt(bundle)) <= 8000`

  - [ ]* 4.4 Write property test for `_init_llmobs` — Property 9: LLMObs init failure is non-fatal
    - **Property 9: Datadog initialization failure does not crash the process**
    - **Validates: Requirements 8.3, 9.5**
    - Use `hypothesis` `@given` with a sampled set of exceptions (`KeyError`, `ConnectionError`, `ImportError`)
    - Patch `LLMObs.enable` to raise the exception; call `_init_llmobs()`; assert no exception propagates

  - [ ]* 4.5 Write unit tests for updated `bedrock.py`
    - Verify `build_prompt` includes all five output key names in its text
    - Verify `build_prompt` includes the uncertainty instruction
    - Verify `invoke_rca` raises `ValueError` on missing required keys
    - Mock `LLMObs` and verify span lifecycle on success and on error
    - _Requirements: 4.1, 5.3, 7.4, 8.1_

- [x] 5. Update `analyzer/app.py`
  - [x] 5.1 Swap `cloudwatch` import for `datadog_mcp` and update `AnalyzeResponse`
    - Replace `import cloudwatch` with `import datadog_mcp`
    - Import `MCPUnavailableError`, `MCPQueryError` from `datadog_mcp`
    - Update `AnalyzeResponse` Pydantic model: replace `impact`/`recommended_fix` with `triage_result`/`recommended_focus`
    - _Requirements: 7.1, 7.4_

  - [x] 5.2 Update `rca_analyze()` orchestration
    - Replace CloudWatch fetch block with `datadog_mcp.fetch_evidence(...)` call
    - Map `MCPUnavailableError` and `MCPQueryError` to HTTP 502
    - Update the "no evidence" guard to check `evidence.logs`, `evidence.monitors`, `evidence.incidents`
    - Update bundle call to `bundle.build_bundle(evidence)`
    - Update response construction to use new field names
    - _Requirements: 3.1, 3.3, 7.1, 7.2, 7.4_

  - [ ]* 5.3 Write property test for `rca_analyze` — Property 6: Request schema accepts all valid input combinations
    - **Property 6: Request schema accepts all valid input combinations**
    - **Validates: Requirements 7.2**
    - Use `hypothesis` `@given` with valid `incident_id`, `service`, datetime `window_start`/`window_end` combinations, with and without `namespace`/`pod_name`
    - Mock `datadog_mcp.fetch_evidence` and `bedrock.invoke_rca` to return valid responses
    - Assert response status is never 422

  - [ ]* 5.4 Write property test for `invoke_rca` result — Property 5: Required RCA output fields always present
    - **Property 5: Required RCA output fields are always present**
    - **Validates: Requirements 5.3, 7.4**
    - Use `hypothesis` `@given` with valid mocked Bedrock responses covering varied field values
    - Assert all five fields (`triage_result`, `root_cause`, `evidence`, `recommended_focus`, `confidence`) are present and non-None

  - [ ]* 5.5 Write property test for triage classification — Property 4: Severe error codes prevent noise classification
    - **Property 4: Severe error codes prevent noise classification**
    - **Validates: Requirements 4.2, 4.3**
    - Use `hypothesis` `@given` with bundles containing `error_type` in `{http_exception, dependency_timeout}` and weak evidence (few logs, no monitors)
    - Assert `triage_result` is `"watch"` or `"needs-attention"`, never `"noise"`

  - [ ]* 5.6 Write unit tests for updated `app.py`
    - Verify `POST /rca/analyze` returns HTTP 502 on `MCPUnavailableError`
    - Verify HTTP 404 on empty evidence (no logs, monitors, incidents)
    - Verify all five response fields on a successful mock call
    - _Requirements: 3.1, 7.1, 7.4_

- [x] 6. Checkpoint — local unit and property tests
  - Run `pytest analyzer/` and verify all tests pass before proceeding to Docker Compose work.

---

### Phase 3 — Local deployment (Docker Compose)

- [x] 7. Update `docker-compose.yml`
  - [x] 7.1 Add `datadog-agent` service
    - Use image `gcr.io/datadoghq/agent:7`; load `DD_API_KEY`, `DD_SITE` from `.env`
    - Enable log collection (`DD_LOGS_ENABLED=true`, `DD_LOGS_CONFIG_CONTAINER_COLLECT_ALL=true`)
    - Enable APM (`DD_APM_ENABLED=true`, `DD_APM_NON_LOCAL_TRAFFIC=true`)
    - Enable Prometheus scrape for faulty-workload `/metrics`
    - Mount `/var/run/docker.sock`, `/proc`, `/sys/fs/cgroup`
    - _Requirements: 2.2, 2.3, 8.2_

  - [x] 7.2 Add `datadog-mcp` service
    - Use `node:20-slim`; install and start `@datadog/mcp` in HTTP mode on port 3000
    - Load `DD_API_KEY`, `DD_APP_KEY`, `DD_SITE` from `.env`
    - Add healthcheck on `http://localhost:3000/health`
    - _Requirements: 3.1_

  - [x] 7.3 Update `faulty-workload` service
    - Add `DD_AGENT_HOST: datadog-agent`, `DD_TRACE_ENABLED: "true"`, `DD_SERVICE`, `DD_ENV`, `DD_VERSION` env vars
    - Add Datadog log autodiscovery label (`com.datadoghq.ad.logs`)
    - Remove CloudWatch AWS credential env vars (keep only those needed for Bedrock)
    - Add `depends_on: datadog-agent`
    - _Requirements: 2.1, 2.2_

  - [x] 7.4 Update `analyzer` service
    - Remove `LOG_GROUP`, `METRIC_NAMESPACE` env vars
    - Add `DD_MCP_TRANSPORT: http`, `DD_MCP_URL: http://datadog-mcp:3000`
    - Add ddtrace APM and LLM Obs env vars (`DD_AGENT_HOST`, `DD_TRACE_ENABLED`, `DD_LLMOBS_ENABLED`, `DD_LLMOBS_AGENTLESS_ENABLED: "false"`, `DD_LLMOBS_ML_APP`)
    - Update `depends_on` to include `datadog-agent` (started) and `datadog-mcp` (healthy)
    - _Requirements: 3.1, 8.1, 8.2_

- [x] 8. Update `scripts/smoke_test.sh`
  - [x] 8.1 Update expected response fields and assertions
    - Replace `REQUIRED_FIELDS` array: swap `impact`/`recommended_fix` → `triage_result`/`recommended_focus`
    - Add assertion: `triage_result` must be one of `noise`, `watch`, `needs-attention`
    - Update ingestion wait comment from "CloudWatch" to "Datadog" (90s default)
    - _Requirements: 4.1, 7.4_

- [x] 9. Checkpoint — run local smoke test end-to-end
  - Start the stack with `docker compose up --build`
  - Run `./scripts/smoke_test.sh` — all assertions must pass
  - Verify all four Datadog signals are visible in the Datadog UI: Logs, Metrics, APM, LLM Obs
  - This checkpoint **gates** Phase 5 (Kubernetes work must not start until this passes)

---

### Phase 4 — Property-based tests

- [x] 10. Implement remaining Hypothesis property tests
  - [x] 10.1 Write property test — Property 1: Structured log fields completeness
    - **Property 1: Structured log fields are always complete**
    - **Validates: Requirements 1.2**
    - In `faulty-workload/` or `analyzer/`, test that `JsonFormatter` emits all seven required fields: `timestamp`, `service`, `severity`, `request_id`, `error_type`, `message`, `trace_id`
    - Use `@given(message=st.text(min_size=1), severity=st.sampled_from([...]), extra=st.fixed_dictionaries({...}))`

  - [x] 10.2 Write property test — Property 2: Trace ID context propagation
    - **Property 2: Trace ID context propagation**
    - **Validates: Requirements 1.3**
    - Use `@given(trace_id=st.text(min_size=1, max_size=64))` — set trace ID in context variable, emit a log record, assert output JSON contains the exact `trace_id`

  - [x] 10.3 Write property test — Property 3: Triage result valid enum
    - **Property 3: Triage result is a valid enum value**
    - **Validates: Requirements 4.1**
    - Use `@given` with mocked Bedrock responses; assert `triage_result in {"noise", "watch", "needs-attention"}` for all valid responses returned by `invoke_rca`

  - [x] 10.4 Collect all property tests into `analyzer/test_properties.py`
    - Consolidate Properties 1–9 into a single test file (or import from submodules)
    - Annotate each test with `# Feature: datadogllm-poc, Property N: <description>` comment
    - Set `@settings(max_examples=100)` on all tests

- [x] 11. Checkpoint — run all property tests
  - Run `pytest analyzer/test_properties.py -v`; all 9 properties must pass before proceeding to Phase 5.

---

### Phase 5 — Kubernetes / EKS (only after local validation)

- [x] 12. Create Kubernetes manifests for Datadog
  - [x] 12.1 Create `k8s/datadog-agent.yaml`
    - DaemonSet using `gcr.io/datadoghq/agent:7`
    - Enable log collection with container autodiscovery
    - Enable APM with non-local traffic
    - Enable Prometheus autodiscovery for faulty-workload `/metrics`
    - Mount host paths: `/var/run/docker.sock`, `/proc`, `/sys/fs/cgroup`
    - Read `DD_API_KEY` from a `datadog-secret` Kubernetes Secret
    - _Requirements: 2.2, 8.2_

  - [x] 12.2 Create `k8s/datadog-mcp.yaml`
    - Deployment (1 replica) running `node:20-slim` with `npx @datadog/mcp --transport http --port 3000`
    - ClusterIP Service on port 3000
    - Mount `DD_API_KEY`, `DD_APP_KEY`, `DD_SITE` from a Kubernetes Secret
    - Add readiness probe on `/health`
    - _Requirements: 3.1_

- [x] 13. Update existing Kubernetes manifests
  - [x] 13.1 Update `k8s/analyzer.yaml`
    - Remove `LOG_GROUP`, `METRIC_NAMESPACE` env vars
    - Remove `DD_LLMOBS_AGENTLESS_ENABLED: "true"` — replace with `"false"` (agent mode)
    - Update `DD_LLMOBS_ML_APP` to `datadogllm-poc`
    - Add `DD_MCP_TRANSPORT: http`, `DD_MCP_URL: http://datadog-mcp:3000`
    - Add `DD_AGENT_HOST`, `DD_TRACE_ENABLED`, `DD_SERVICE`, `DD_ENV` env vars
    - Update IRSA annotation comment: remove CloudWatch policies note
    - _Requirements: 3.1, 8.1, 2.5_

  - [x] 13.2 Update `k8s/faulty-workload.yaml`
    - Add `DD_AGENT_HOST`, `DD_SERVICE`, `DD_ENV`, `DD_VERSION` env vars pointing to the Datadog Agent DaemonSet
    - Remove CloudWatch-specific comment from `FAULT_SAMPLE_RATE`
    - _Requirements: 2.1, 1.3_

- [x] 14. Remove obsolete CloudWatch assets
  - [x] 14.1 Delete `k8s/otel-collector.yaml`
    - This DaemonSet is replaced by the Datadog Agent DaemonSet
    - _Requirements: 2.5_

  - [x] 14.2 Delete `cloudwatch/` folder (`alarms.json`, `dashboard.json`)
    - _Requirements: 2.5_

---

### Phase 6 — Cleanup

- [x] 15. Delete `analyzer/cloudwatch.py`
  - The module is fully replaced by `analyzer/datadog_mcp.py`
  - Verify no remaining imports reference `cloudwatch` in any analyzer file
  - _Requirements: 2.5_

- [x] 16. Update `analyzer/templates/index.html`
  - [x] 16.1 Update `renderRCA()` JavaScript function
    - Replace `data.impact` → `data.triage_result`
    - Replace `data.recommended_fix` → `data.recommended_focus`
    - Update label strings accordingly (`"Impact"` → `"Triage"`, `"Recommended Fix"` → `"Recommended Focus"`)
    - _Requirements: 4.1, 7.4_

  - [x] 16.2 Add triage badge with colour coding
    - Display `triage_result` as a coloured badge: `noise` = grey, `watch` = amber, `needs-attention` = red
    - _Requirements: 4.1_

- [x] 17. Update `README.md`
  - [x] 17.1 Remove all CloudWatch references
    - Remove CloudWatch prerequisites, IAM policies for CloudWatch, `LOG_GROUP`/`METRIC_NAMESPACE` configuration steps
    - _Requirements: 2.5_

  - [x] 17.2 Update quickstart, prerequisites, and architecture for Datadog
    - Add Datadog account prerequisite with `DD_API_KEY` and `DD_APP_KEY` setup instructions
    - Update architecture diagram to show Datadog Agent and Datadog MCP replacing otel-collector and CloudWatch
    - Update IAM section: Bedrock-only IRSA (no CloudWatch policies)
    - Update local quickstart steps to reference the new Docker Compose services and 90s ingestion wait
    - _Requirements: 2.1, 3.1_

- [x] 18. Final checkpoint — full regression
  - Run `pytest analyzer/ -v` — all tests pass
  - Run `./scripts/smoke_test.sh` — smoke test passes end-to-end

---

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP iteration
- **Phase gate**: Tasks 12–18 (Kubernetes + cleanup) must not start until task 9 (local smoke test) and task 11 (property tests) both pass
- Property tests (Properties 1–9) are distributed near the code they validate; task 10 consolidates the remaining ones not co-located with implementation tasks
- All property tests use `@settings(max_examples=100)` and the tag format `# Feature: datadogllm-poc, Property N: <description>`
- The `stdio` MCP transport is only used for bare-metal local development; all Docker/k8s environments use the `http` transport

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3"] },
    { "id": 1, "tasks": ["2.1", "3.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "4.1", "4.2"] },
    { "id": 3, "tasks": ["2.4", "3.2", "3.3", "4.3", "4.4", "4.5"] },
    { "id": 4, "tasks": ["2.5", "5.1"] },
    { "id": 5, "tasks": ["5.2", "5.3", "5.4", "5.5", "5.6"] },
    { "id": 6, "tasks": ["7.1", "7.2", "8.1", "10.1", "10.2", "10.3", "10.4"] },
    { "id": 7, "tasks": ["7.3", "7.4"] },
    { "id": 8, "tasks": ["12.1", "12.2", "13.1", "13.2", "14.1", "14.2", "15"] },
    { "id": 9, "tasks": ["16.1", "16.2", "17.1", "17.2"] }
  ]
}
```
