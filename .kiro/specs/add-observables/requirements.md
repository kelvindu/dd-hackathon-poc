# Requirements Document

## Introduction
The system shall extend the existing `faulty-workload` service by running three additional variant pods on EKS/EC2: `customers`, `orders`, and `auth`. Each variant reuses the same fault-injection architecture (FastAPI + structured JSON logging + Prometheus metrics + trace propagation) but exposes a domain-themed fault profile. All three are purely observable targets — they exist to generate telemetry for the analyzer/POC pipeline, not to serve real business traffic.

## Glossary
- **Faulty workload**: A synthetic Kubernetes workload that emits controlled failures and anomalies for POC validation.
- **Workload variant**: A configuration-driven instance of the faulty-workload service with a domain-specific fault profile (e.g. customers, orders, auth).
- **Trace ID**: A unique identifier propagated across logs, metrics, and traces to correlate telemetry for a single request.
- **Observable target**: A workload whose sole purpose is to emit telemetry consumed by the analyzer pipeline.
- **Customers variant**: A faulty-workload instance themed around customer-service failure patterns.
- **Orders variant**: A faulty-workload instance themed around order-processing failure patterns.
- **Auth variant**: A faulty-workload instance themed around authentication/authorization failure patterns.

## Requirements

### Requirement 1: Customers variant
**User Story:** As a platform engineer, I want a `customers` variant of the faulty-workload that produces customer-service themed failures, so that I can observe and validate one realistic incident pattern as an observable target.

#### Acceptance Criteria
1. WHEN the `customers` variant runs, THE SYSTEM SHALL reuse the existing faulty-workload fault-injection architecture and emit intermittent warnings, transient errors, and latency spikes themed around customer-service scenarios.
2. WHEN the `customers` variant emits telemetry, THE SYSTEM SHALL include structured JSON fields for timestamp, service name, workload_family, severity, request_id, error_type, message, and trace_id consistent with the existing faulty-workload telemetry contract.
3. WHEN a `customers` request is processed, THE SYSTEM SHALL propagate trace_id across logs, metrics, and traces; IF trace propagation fails, THE SYSTEM SHALL continue processing the request and log the propagation failure.
4. WHEN the `customers` variant is idle, THE SYSTEM SHALL emit either background health signals or low-volume metrics at a configurable interval.

### Requirement 2: Orders variant
**User Story:** As a platform engineer, I want an `orders` variant of the faulty-workload that produces order-processing themed failures, so that I can observe and validate a second distinct incident pattern as an observable target.

#### Acceptance Criteria
1. WHEN the `orders` variant runs, THE SYSTEM SHALL reuse the existing faulty-workload fault-injection architecture and emit intermittent dependency timeouts, processing errors, and latency spikes themed around order-processing scenarios.
2. WHEN the `orders` variant emits telemetry, THE SYSTEM SHALL include structured JSON fields for timestamp, service name, workload_family, severity, request_id, error_type, message, and trace_id consistent with the existing faulty-workload telemetry contract.
3. WHEN an `orders` request is processed, THE SYSTEM SHALL propagate trace_id across logs, metrics, and traces; IF trace propagation fails, THE SYSTEM SHALL continue processing the request and log the propagation failure.
4. WHEN the `orders` variant is idle, THE SYSTEM SHALL emit either background health signals or low-volume metrics at a configurable interval.

### Requirement 3: Auth variant
**User Story:** As a platform engineer, I want an `auth` variant of the faulty-workload that produces authentication/authorization themed failures, so that I can observe and validate a third distinct incident pattern as an observable target.

#### Acceptance Criteria
1. WHEN the `auth` variant runs, THE SYSTEM SHALL reuse the existing faulty-workload fault-injection architecture and emit intermittent authorization failures, token validation errors, and latency spikes with variance in frequency and severity similar to other faulty-workload variants.
2. WHEN the `auth` variant emits telemetry, THE SYSTEM SHALL include structured JSON fields for timestamp, service name, workload_family, severity, request_id, error_type, message, and trace_id consistent with the existing faulty-workload telemetry contract.
3. WHEN an `auth` request is processed, THE SYSTEM SHALL propagate trace_id across logs, metrics, and traces; IF trace propagation fails, THE SYSTEM SHALL continue processing the request and log the propagation failure.
4. WHEN the `auth` variant is idle, THE SYSTEM SHALL emit either background health signals or low-volume metrics at a configurable interval.

### Requirement 4: Variant separation
**User Story:** As a platform engineer, I want each workload variant to remain distinguishable in telemetry, so that I can compare incidents across observable targets without ambiguity.

#### Acceptance Criteria
1. WHEN telemetry is emitted by any variant, THE SYSTEM SHALL preserve workload_family as a searchable dimension.
2. WHEN a single incident affects one variant, THE SYSTEM SHALL not require unrelated variants to be involved in the analysis.
3. WHEN more than one variant fails at once, THE SYSTEM SHALL allow the incidents to be correlated by shared trace or time window.

### Requirement 5: Shared telemetry contract
**User Story:** As a platform engineer, I want all three variants to follow the same telemetry contract as the existing faulty-workload, so that the POC remains consistent and analyzable by the same pipeline.

#### Acceptance Criteria
1. WHEN any variant emits a log line, THE SYSTEM SHALL format it as structured JSON using the same logger module as the existing faulty-workload.
2. WHEN any variant emits a metric, THE SYSTEM SHALL keep the metric names and labels low-cardinality and expose them via the same Prometheus endpoint pattern.
3. WHEN any variant emits traces, THE SYSTEM SHALL preserve trace_id across request boundaries where applicable.
4. WHEN any variant emits an error, THE SYSTEM SHALL include a stable error_type value.

### Requirement 6: Configuration and deployment
**User Story:** As a platform engineer, I want each variant to be deployable as a separate pod using the same Docker image with environment-based configuration, so that I can run all three observable targets without maintaining separate codebases.

#### Acceptance Criteria
1. WHEN a variant is deployed, THE SYSTEM SHALL differentiate behavior through environment variables (e.g. WORKLOAD_FAMILY) rather than separate source trees.
2. WHEN the workloads are configured, THE SYSTEM SHALL allow failure frequency to be adjusted per variant via environment variables.
3. WHEN the POC is idle, THE SYSTEM SHALL continue to emit healthcheck signals at a configurable interval defined in the `.env` file.
4. WHEN a variant is healthy, THE SYSTEM SHALL still emit periodic health signals at the configured healthcheck interval for visibility.
