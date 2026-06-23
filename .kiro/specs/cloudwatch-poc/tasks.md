# Implementation Plan: CloudWatch POC

## Overview

This plan implements a Kubernetes observability proof-of-concept in four phases: a faulty workload pod that emits controlled failures, a telemetry pipeline shipping to CloudWatch, an analyzer service that queries CloudWatch and invokes Bedrock for root-cause analysis, and the Kubernetes manifests plus a simple UI for end-to-end validation.

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

## Notes

- Tasks 1–4 are complete; execution should resume from task 5.
- The design document uses no formal Correctness Properties section, so no property-based test sub-tasks are included — integration and smoke tests cover correctness instead.
- All tasks are coding tasks only; deployment and manual validation are handled by the smoke test script (task 17).
- Each task references the requirements document for traceability.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["5"] },
    { "id": 1, "tasks": ["6", "9"] },
    { "id": 2, "tasks": ["7", "8", "10"] },
    { "id": 3, "tasks": ["11", "15"] },
    { "id": 4, "tasks": ["12"] },
    { "id": 5, "tasks": ["13", "16"] },
    { "id": 6, "tasks": ["14"] },
    { "id": 7, "tasks": ["17", "18"] }
  ]
}
```
