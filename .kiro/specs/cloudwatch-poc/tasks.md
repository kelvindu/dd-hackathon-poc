# Implementation Plan: CloudWatch POC → Datadog Migration

## Overview

This plan implements a Kubernetes observability proof-of-concept with Datadog as the single telemetry backend. The workload emits controlled failures; the Datadog Agent collects logs, metrics, and APM traces from the cluster; an analyzer service queries Datadog for incident evidence and invokes Bedrock (instrumented with Datadog LLM Observability) to produce a structured root-cause analysis.

Tasks 1–20 are complete and cover the faulty workload, the original CloudWatch pipeline, the analyzer scaffolding, Bedrock integration, and the initial Datadog LLM Observability wiring. Tasks 21–26 migrate the evidence-query layer from CloudWatch to Datadog and update all supporting configuration to match the final Datadog-only design.

## Tasks

- [x] 1. Scaffold the `faulty-workload` Python/FastAPI service
  - Create `faulty-workload/app.py` with a basic FastAPI app and a `GET /` health check
  - Create `faulty-workload/Dockerfile` using a slim Python base image
  - Create `faulty-workload/requirements.txt` with `fastapi`, `uvicorn`, `boto3`, `prometheus-client`
  - _Requirements: Functional Requirements (workload generates events)_

- [x] 2. Implement fault injection logic
  - Add `faulty-workload/faults.py` with configurable fault injection:
    - 5% random HTTP 500 errors
    - Occasional 2–5s latency spikes (every ~20 requests)
    - Dependency timeout simulation every N requests (configurable via env var)
    - Memory pressure warning when a counter threshold is crossed
    - Bad payload warning for missing/malformed request fields
  - Wire fault injection into the request handler in `app.py`
  - _Requirements: Functional Requirements (workload generates intermittent warnings, transient errors, latency spikes, dependency timeouts)_

- [x] 3. Add structured JSON logger
  - Create `faulty-workload/logger.py` with a custom JSON formatter
  - Each log record must include: `timestamp`, `service`, `severity`, `trace_id`, `request_id`, `error_type`, `message`
  - Use Python's `logging` module with a `StreamHandler` to stdout
  - _Requirements: Functional Requirements (structured JSON logs)_

- [x] 4. Add Prometheus metrics
  - Create `faulty-workload/metrics.py` using `prometheus-client`
  - Define counters/gauges: `request_count`, `warning_count`, `error_count`, `latency_ms` (histogram), `timeout_count`, `restart_count`
  - Expose `/metrics` endpoint via `prometheus-client`'s ASGI middleware
  - _Requirements: Functional Requirements (basic metrics)_

- [x] 5. Propagate trace_id through the request path
  - On each request, extract `X-Trace-ID` header or generate a new UUID as `trace_id`
  - Extract or generate `X-Request-ID` as `request_id`
  - Store both in a request-scoped context (Python `contextvars`)
  - Pass `trace_id` and `request_id` to every log call and metric label
  - _Requirements: Functional Requirements (propagate trace_id across logs, metrics, and traces)_

- [x] 6. Configure the OTEL Collector to ship to CloudWatch
  - Create `k8s/otel-collector.yaml` as a DaemonSet with the `aws-otel-collector` image
  - Configure the pipeline: `receivers: otlp`, `exporters: awscloudwatchlogs`, `awsemf`, `awsxray`
  - Set log group name to `/poc/faulty-workload` and metric namespace to `POC/FaultyWorkload`
  - _Requirements: Functional Requirements (telemetry observable in CloudWatch)_

- [x] 7. Add CloudWatch dashboard definition
  - Create `cloudwatch/dashboard.json` with widgets for:
    - Error rate (errors / requests over time)
    - Warning count
    - Latency P50 and P99
    - Pod restart count
  - _Requirements: Functional Requirements (telemetry observable in CloudWatch)_

- [x] 8. Add CloudWatch Alarms
  - Create `cloudwatch/alarms.json` with alarm definitions for:
    - `error_count` exceeds threshold (e.g. >10 in 5 minutes)
    - `latency_ms` P99 exceeds 3000ms
    - `restart_count` exceeds 2 in 10 minutes
  - _Requirements: Non-Functional Requirements_

- [x] 9. Scaffold the `analyzer` Python/FastAPI service
  - Create `analyzer/app.py` with FastAPI and `POST /rca/analyze` stub that returns 200
  - Create `analyzer/Dockerfile`
  - Create `analyzer/requirements.txt` with `fastapi`, `uvicorn`, `boto3`
  - Define Pydantic request model: `incident_id`, `service`, `window_start`, `window_end`, `namespace` (optional), `pod_name` (optional)
  - Define Pydantic response model: `root_cause`, `evidence`, `impact`, `recommended_fix`, `confidence`
  - _Requirements: Functional Requirements (analyzer service exposes POST /rca/analyze, accepts incident metadata)_

- [x] 10. Implement CloudWatch Logs Insights query logic
  - Create `analyzer/cloudwatch.py`
  - Implement `query_logs(log_group, window_start, window_end, pod_name)` using `boto3` CloudWatch Logs Insights
  - Query for ERROR and WARNING severity events; limit to top 20 results to control cost
  - Return a list of dicts with `timestamp`, `severity`, `error_type`, `message`, `trace_id`
  - _Requirements: Functional Requirements (query CloudWatch for relevant incident window)_

- [x] 11. Implement CloudWatch Metrics delta query
  - Add `query_metrics(namespace, service, window_start, window_end)` to `analyzer/cloudwatch.py`
  - Use `boto3` `get_metric_statistics` to fetch `request_count`, `error_count`, `warning_count`, `latency_ms` for the window
  - Compute delta (max minus min) for each metric over the window
  - _Requirements: Functional Requirements (query CloudWatch for relevant incident window)_

- [x] 12. Build the compact incident bundle
  - Create `analyzer/bundle.py` with `build_bundle(logs, metrics, traces)` function
  - Cap logs at top 10 most severe entries
  - Include metric deltas as key-value pairs
  - Include up to 5 trace anomaly summaries if available
  - Return a dict that fits in a short Bedrock prompt
  - _Requirements: Functional Requirements (compress observed data into compact bundle before sending to Bedrock)_

- [x] 13. Build the Bedrock prompt and call Bedrock
  - Create `analyzer/bedrock.py`
  - Implement `build_prompt(bundle)` that produces the compact structured prompt
  - Implement `invoke_rca(bundle)` using `boto3` `bedrock-runtime` client with Claude model
  - Request JSON-only output; parse the response and validate keys: `root_cause`, `evidence`, `impact`, `recommended_fix`, `confidence`
  - _Requirements: Functional Requirements (Bedrock returns JSON-only RCA response)_

- [x] 14. Wire everything into `POST /rca/analyze`
  - In `analyzer/app.py`, connect `cloudwatch.query_logs`, `cloudwatch.query_metrics`, `bundle.build_bundle`, and `bedrock.invoke_rca`
  - Return the Bedrock RCA JSON as the HTTP response
  - Add basic error handling: if CloudWatch returns no data, return a 404 with a descriptive message
  - _Requirements: Functional Requirements (analyzer service queries CloudWatch and returns RCA)_

- [x] 15. Write Kubernetes manifests
  - Create `k8s/faulty-workload.yaml`: Deployment + Service, env vars for fault injection rates, resource limits set low for POC
  - Create `k8s/analyzer.yaml`: Deployment + Service with IAM role annotation for CloudWatch and Bedrock access
  - _Requirements: Non-Functional Requirements (EC2-hosted Kubernetes environment)_

- [x] 16. Add a simple incident UI
  - Create `analyzer/templates/index.html` with three panels:
    - Incident selector form (incident_id, service, time window inputs)
    - Evidence timeline showing log + metric excerpts returned in the response
    - Bedrock RCA output card rendering `root_cause`, `evidence`, `impact`, `recommended_fix`, `confidence`
  - Serve via a `GET /` route using FastAPI's Jinja2 template support
  - _Requirements: Acceptance Criteria_

- [x] 17. Validate end-to-end incident flow
  - Write a `scripts/smoke_test.sh` that:
    - Sends 50 requests to the faulty-workload to generate faults
    - Waits 60 seconds for CloudWatch ingestion
    - Calls `POST /rca/analyze` with the matching time window
    - Asserts the response contains all five RCA fields and `confidence` > 0
  - _Requirements: Acceptance Criteria_

- [x] 18. Cost and volume tuning
  - Set CloudWatch log retention to 3 days in the OTEL collector config
  - Add a `FAULT_SAMPLE_RATE` env var to the workload to reduce log noise in non-fault requests
  - Verify the Bedrock prompt stays under 500 tokens by logging token count in `bedrock.py`
  - _Requirements: Non-Functional Requirements_

- [x] 19. Create dotenv deployments
  - Make bedrock llm model configurable via .env
  - Pull every constant placeholders in the previous tasks into .env
  - cleanup the rest of app / hardcoded value / constants you see are better to be .env

- [x] 20. Add Datadog LLM Observability to the Bedrock call path
  - Add `ddtrace[openai]==2.9.4` to `analyzer/requirements.txt`
  - Add `DD_API_KEY`, `DD_SITE`, `DD_LLMOBS_ENABLED`, `DD_LLMOBS_ML_APP`, and `DD_LLMOBS_AGENTLESS_ENABLED` to `.env` and `.env.example`
  - In `analyzer/bedrock.py`, conditionally initialise Datadog LLMObs at import time (no-op when `DD_LLMOBS_ENABLED` is not `"true"`)
  - Wrap `invoke_rca` with a `LLMObs.llm` span: record model provider (`"bedrock"`), model name (`BEDROCK_MODEL_ID`), input prompt, output text, and approximate token counts
  - Capture error metadata on the span when `invoke_model` raises or the JSON parse fails; ensure exceptions still propagate so the caller gets a 502
  - Add `DD_API_KEY` and `DD_LLMOBS_*` env entries to `k8s/analyzer.yaml` (values as comments / placeholders)
  - _Requirements: Functional Requirements (Bedrock call path instrumented with Datadog LLM Observability); Non-Functional Requirements (graceful degradation when Datadog unavailable)_

- [ ] 21. Replace `analyzer/cloudwatch.py` with `analyzer/datadog_client.py`
  - Create `analyzer/datadog_client.py`; the old `cloudwatch.py` may be kept for reference but must not be imported anywhere
  - Read `DD_API_KEY` and `DD_APP_KEY` from environment variables; raise a clear `RuntimeError` at call time if either is missing
  - Implement `query_logs(service, window_start, window_end, pod_name=None) -> List[Dict[str, str]]`:
    - Call the Datadog Logs Search API (`POST /api/v2/logs/events/search`)
    - Filter query: `service:<service> (status:error OR status:warning)`, with optional `pod_name:<pod_name>` clause appended
    - Convert `window_start` / `window_end` to ISO-8601 strings for the `filter.from` / `filter.to` fields
    - Parse each log event into the same shape as the old CloudWatch module: `{timestamp, severity, error_type, message, trace_id}`; map Datadog `status` → `severity` (upper-case), pull `error_type`, `message`, and `trace_id` from the log attributes; fall back to empty string for absent fields
    - Limit to 20 results via the `page.limit` parameter
  - Implement `query_metrics(service, window_start, window_end) -> Dict[str, float]`:
    - Call the Datadog Metrics Query API (`GET /api/v1/query`) once per metric using the `avg` rollup for counts and the `p50`/`p99` rollup for latency
    - Metrics to query: `faulty_workload.request_count`, `faulty_workload.error_count`, `faulty_workload.warning_count`, `faulty_workload.timeout_count`, `faulty_workload.latency_ms`; filter each by `service:<service>`
    - Compute delta (max datapoint − min datapoint) for count metrics and return the last non-null rollup value for `latency_p50_ms` / `latency_p99_ms`; return `0.0` when no datapoints are present
    - Return a dict with the same six keys as the old CloudWatch module: `request_count`, `error_count`, `warning_count`, `timeout_count`, `latency_p50_ms`, `latency_p99_ms`
  - Use the `requests` library for all HTTP calls; set a 30-second timeout; raise `RuntimeError` on non-2xx responses so the caller receives a 502
  - _Requirements: Functional Requirements (analyzer service shall query Datadog for the relevant incident window)_

- [ ] 22. Update `analyzer/app.py` to use `datadog_client` instead of `cloudwatch`
  - Replace `import cloudwatch` with `import datadog_client`
  - Update the `rca_analyze` handler to call `datadog_client.query_logs(service, window_start, window_end, pod_name)` and `datadog_client.query_metrics(service, window_start, window_end)` — note the new signatures no longer take `log_group` or `namespace`
  - Remove the `log_group` and `metric_namespace` locals and the corresponding `os.environ.get("LOG_GROUP", ...)` / `os.environ.get("METRIC_NAMESPACE", ...)` reads
  - Add `DD_APP_KEY` to the env reads validated at startup (alongside the existing `DD_API_KEY` check in `bedrock.py`)
  - Update the 404 guard message from "No CloudWatch data found…" to "No Datadog data found for the specified window"
  - Update HTTP error handling to surface Datadog API error codes: wrap `RuntimeError` from `datadog_client` into a 502; if the error message contains "401" or "403" include a hint about invalid credentials; if it contains "429" include a hint about rate limiting
  - _Requirements: Functional Requirements (analyzer service shall query Datadog for the relevant incident window)_

- [ ] 23. Migrate `k8s/otel-collector.yaml` from CloudWatch exporters to Datadog
  - Replace the `amazon/aws-otel-collector` container image with `otel/opentelemetry-collector-contrib:latest` (which includes the Datadog exporter)
  - Remove the `awscloudwatchlogs`, `awsemf`, and `awsxray` exporter blocks from the ConfigMap
  - Add a `datadog` exporter block pointing to `api.${DD_SITE}` with the API key sourced from the `DD_API_KEY` env var; enable `logs`, `metrics`, and `traces` sub-sections
  - Update all three pipelines (`logs`, `metrics`, `traces`) to use `exporters: [datadog]`
  - Remove the IRSA `eks.amazonaws.com/role-arn` annotation from the `otel-collector` ServiceAccount — CloudWatch/X-Ray IAM access is no longer needed
  - Add a `DD_API_KEY` env var to the DaemonSet container sourced from the `datadog-secret` Kubernetes Secret (same secret already used by `k8s/analyzer.yaml`)
  - Add a `DD_SITE` env var defaulting to `datadoghq.com`
  - Keep the existing OTLP receiver ports (4317 gRPC, 4318 HTTP), ClusterRole, ClusterRoleBinding, and Service objects unchanged
  - _Requirements: Functional Requirements (telemetry shall be observable in Datadog); Design Constraint (route all workload telemetry into Datadog as the single observability backend)_

- [ ] 24. Add `DD_APP_KEY` to env files and clean up obsolete CloudWatch vars in `k8s/analyzer.yaml`
  - Add `DD_APP_KEY=` (empty placeholder) to `.env` and `.env.example` in the Datadog section, below `DD_API_KEY`
  - In `.env.example`, add a comment explaining that `DD_APP_KEY` is required by the Datadog Metrics and Logs Search APIs
  - In `k8s/analyzer.yaml`, add a `DD_APP_KEY` env entry sourced from `datadog-secret` (key: `app-key`, optional: true) alongside the existing `DD_API_KEY` entry
  - Comment out (do not delete) the `LOG_GROUP` and `METRIC_NAMESPACE` env entries in `k8s/analyzer.yaml` with a note that they are superseded by Datadog
  - In `.env` and `.env.example`, comment out `LOG_GROUP` and `METRIC_NAMESPACE` with a note that they are no longer used by the analyzer
  - _Requirements: Functional Requirements (analyzer service shall query Datadog for the relevant incident window)_

- [ ] 25. Update `scripts/smoke_test.sh` to validate Datadog ingestion
  - Replace the comment referencing CloudWatch ingestion with one referencing Datadog ingestion
  - Change the default `SMOKE_INGESTION_WAIT_SECS` from `60` to `30` (Datadog ingestion is typically faster; keep it overridable via env var)
  - After the sleep, add a validation step that calls the Datadog Logs Search API directly from the script to confirm at least one log event with `service:faulty-workload` is visible for the test window; use `DD_API_KEY` and `DD_APP_KEY` from the environment
  - If the Datadog check returns zero results, print a warning but do not fail the script — the RCA call may still succeed if the analyzer already fetched the data; add a `SKIP_DD_CHECK=false` env var guard so this check can be disabled
  - Update the ingestion wait echo message from "Waiting … for CloudWatch ingestion" to "Waiting … for Datadog ingestion"
  - _Requirements: Acceptance Criteria (Datadog shows the generated events, metrics, and traces; the analyzer can retrieve the incident evidence from Datadog)_

- [ ] 26. Add Datadog API client dependency to `analyzer/requirements.txt`
  - Add `datadog-api-client==2.26.0` to `analyzer/requirements.txt` as the primary Datadog SDK
  - Alternatively, if `datadog_client.py` (task 21) is implemented using the plain `requests` library, add `requests==2.32.3` instead — whichever library is actually used in the implementation
  - Ensure the chosen dependency does not conflict with existing pins (`fastapi==0.111.0`, `uvicorn==0.29.0`, `boto3==1.34.110`, `ddtrace[openai]==2.9.4`)
  - _Requirements: Functional Requirements (analyzer service shall query Datadog for the relevant incident window)_

## Notes

- Tasks 1–20 are complete. The faulty workload, telemetry pipeline, analyzer scaffolding, Bedrock integration, and Datadog LLM Observability wiring are all done.
- Tasks 21–26 are the remaining work. They migrate the evidence-query layer from CloudWatch (`analyzer/cloudwatch.py`) to Datadog (`analyzer/datadog_client.py`) and update all supporting files to match the Datadog-only architecture mandated by the current design.
- Tasks 21, 23, 24, and 26 are independent of each other and can be executed in parallel.
- Task 22 depends on task 21 (the new `datadog_client` module must exist before `app.py` can import it).
- Task 25 depends on tasks 21–24 being complete so the full end-to-end path through Datadog is testable.
- The design document uses no formal Correctness Properties section, so no property-based test sub-tasks are included — integration and smoke tests cover correctness instead.
- `bundle.py` requires no changes: the output shapes of `datadog_client.query_logs` and `datadog_client.query_metrics` are defined in task 21 to be identical to the old CloudWatch shapes.
- `bedrock.py` requires no changes.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1", "2", "3", "4"] },
    { "id": 1, "tasks": ["5"] },
    { "id": 2, "tasks": ["6", "9"] },
    { "id": 3, "tasks": ["7", "8", "10"] },
    { "id": 4, "tasks": ["11", "15"] },
    { "id": 5, "tasks": ["12"] },
    { "id": 6, "tasks": ["13", "16"] },
    { "id": 7, "tasks": ["14"] },
    { "id": 8, "tasks": ["17", "18"] },
    { "id": 9, "tasks": ["19", "20"] },
    { "id": 10, "tasks": ["21", "23", "24", "26"] },
    { "id": 11, "tasks": ["22"] },
    { "id": 12, "tasks": ["25"] }
  ]
}
```
