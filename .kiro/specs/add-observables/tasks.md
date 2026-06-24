# Implementation Plan: Add Observables

## Overview

Extend the existing `faulty-workload` service to support three additional workload variants (`customers`, `orders`, `auth`) differentiated by a `WORKLOAD_FAMILY` environment variable. This involves creating a fault profile registry, modifying the app startup to apply profiles, adding `workload_family` to logs and metrics, creating K8s manifests, and adding docker-compose services for local dev.

## Tasks

- [x] 1. Create fault profile registry and wire into app startup
  - [x] 1.1 Create `faulty-workload/profiles.py` with fault profile registry
    - Define `PROFILES` dict mapping `WORKLOAD_FAMILY` values (`default`, `customers`, `orders`, `auth`) to fault parameter dicts
    - Implement `get_profile(family: str) -> dict[str, str]` with fallback to `"default"` for unknown families
    - All values must be strings parseable as their expected numeric types
    - _Requirements: 1.1, 2.1, 3.1, 6.1_

  - [x] 1.2 Modify `faulty-workload/app.py` to apply profile at startup
    - Read `WORKLOAD_FAMILY` env var (default: `"default"`)
    - Call `get_profile()` and apply values using `os.environ.setdefault()` so explicit env vars take precedence
    - Place profile application after `load_dotenv()` but before `faults.py` imports (which read env vars at import time)
    - _Requirements: 6.1, 6.2_

  - [x]* 1.3 Write property test for profile resolution (Property 1)
    - **Property 1: Profile resolution returns valid configuration**
    - **Validates: Requirements 1.1, 2.1, 3.1**
    - Use Hypothesis `@given` with `sampled_from` for known families plus `text()` for arbitrary strings
    - Assert returned dict contains all 6 required keys with values parseable as float/int

  - [x]* 1.4 Write property test for env-var override precedence (Property 5)
    - **Property 5: Environment variable override precedence**
    - **Validates: Requirements 6.2**
    - Use Hypothesis to generate arbitrary subsets of fault parameters to pre-set as env vars
    - Assert pre-set values are preserved and unset values resolve to profile defaults

- [x] 2. Extend logger with `workload_family` field
  - [x] 2.1 Modify `faulty-workload/logger.py` to include `workload_family` in JSON output
    - Add `workload_family` field to the `payload` dict in `JsonFormatter.format()`
    - Source value from `os.environ.get("WORKLOAD_FAMILY", "default")`
    - _Requirements: 1.2, 2.2, 3.2, 4.1, 5.1_

  - [x]* 2.2 Write property test for log format completeness (Property 2)
    - **Property 2: Log format completeness**
    - **Validates: Requirements 1.2, 2.2, 3.2, 4.1, 5.1**
    - Use Hypothesis to generate arbitrary severity, message, trace_id, request_id, error_type, and workload_family values
    - Assert output is valid JSON containing all required fields: `timestamp`, `service`, `workload_family`, `severity`, `trace_id`, `request_id`, `error_type`, `message`

  - [x]* 2.3 Write property test for trace ID propagation (Property 3)
    - **Property 3: Trace ID propagation round-trip**
    - **Validates: Requirements 1.3, 2.3, 3.3, 5.3**
    - Use Hypothesis to generate non-empty trace_id strings
    - Set `trace_id_var` context variable, format a log record, assert JSON `trace_id` field matches

  - [x]* 2.4 Write property test for error-type stability (Property 4)
    - **Property 4: Error-type stability for faults**
    - **Validates: Requirements 5.4**
    - Use Hypothesis to generate `FaultResult` instances with non-empty `warning_type`
    - Assert formatted log JSON contains `error_type` equal to the `warning_type`

- [x] 3. Extend metrics with `workload_family` label
  - [x] 3.1 Modify `faulty-workload/metrics.py` to add `workload_family` label to all counters and histogram
    - Add `labelnames=["workload_family"]` to `request_count`, `error_count`, `timeout_count`, and `latency_ms`
    - Read `_WORKLOAD_FAMILY` from env at module level
    - _Requirements: 4.1, 5.2_

  - [x] 3.2 Update `faulty-workload/app.py` metric call sites to use `.labels(workload_family=...)`
    - Update `request_count.inc()` → `request_count.labels(workload_family=_WORKLOAD_FAMILY).inc()`
    - Apply same pattern to `error_count`, `timeout_count`, `latency_ms`
    - `warning_count` already has `warning_type` label; add `workload_family` as additional label
    - _Requirements: 4.1, 5.2_

  - [x]* 3.3 Write unit tests for metrics `workload_family` label
    - Verify counters and histogram expose `workload_family` label
    - Verify label value matches `WORKLOAD_FAMILY` env var
    - _Requirements: 4.1, 5.2_

- [x] 4. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Create K8s deployment manifests for variant workloads
  - [x] 5.1 Create `k8s/observable-workloads.yaml` with Deployment and Service for each variant
    - Three Deployment+Service pairs: `customers-workload`, `orders-workload`, `auth-workload`
    - Each uses image `faulty-workload:latest` with `WORKLOAD_FAMILY` env var set accordingly
    - Set `SERVICE_NAME` and `DD_SERVICE` per variant
    - Omit fault parameter env vars (let `profiles.py` provide defaults)
    - Include readiness/liveness probes and resource limits matching existing manifest
    - _Requirements: 1.1, 2.1, 3.1, 6.1, 6.2_

- [x] 6. Add docker-compose services for local development
  - [x] 6.1 Add `customers-workload`, `orders-workload`, `auth-workload` services to `docker-compose.yml`
    - All use `build: ./faulty-workload`
    - Map to different host ports (8081, 8082, 8083)
    - Set `WORKLOAD_FAMILY` and `SERVICE_NAME` env vars per variant
    - Set `DD_SERVICE` matching service name
    - Add Datadog log labels per service
    - Depend on `datadog-agent`
    - _Requirements: 6.1, 6.2_

  - [x] 6.2 Update Datadog agent `DD_PROMETHEUS_SCRAPE_CHECKS` to include variant endpoints
    - Add scrape URLs for all three variant services
    - _Requirements: 4.1, 5.2_

- [x] 7. Integration wiring and final validation
  - [x]* 7.1 Write unit tests for profile application and fallback behavior
    - Test `get_profile()` returns expected dict for each known family
    - Test unknown family falls back to `"default"`
    - Test env-var override wins over profile default
    - _Requirements: 6.1, 6.2_

  - [x]* 7.2 Write integration test verifying variant telemetry is distinguishable
    - Start two variants, assert logs contain different `workload_family` values
    - Verify Prometheus `/metrics` endpoint includes `workload_family` label
    - _Requirements: 4.1, 4.2, 4.3_

- [x] 8. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties defined in the design (Hypothesis, min 100 examples)
- Unit tests validate specific examples and edge cases
- The existing `faulty-workload` deployment remains unchanged; all changes are additive
- `faults.py` reads env vars at import time, so profile application in `app.py` must happen before importing `faults`

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1", "3.1"] },
    { "id": 2, "tasks": ["1.3", "1.4", "2.2", "2.3", "2.4", "3.2"] },
    { "id": 3, "tasks": ["3.3", "5.1", "6.1"] },
    { "id": 4, "tasks": ["6.2", "7.1"] },
    { "id": 5, "tasks": ["7.2"] }
  ]
}
```
