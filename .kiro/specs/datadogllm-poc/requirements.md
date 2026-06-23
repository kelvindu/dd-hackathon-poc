# Requirements Document

## Introduction
The system shall run noisy Kubernetes workloads on EKS/EC2, store all telemetry in Datadog, and use a Bedrock-backed analyzer that depends on Datadog MCP server calls for triage, root cause analysis, and recommendations. The analyzer shall remain evidence-driven, cost-aware, and conservative when data is incomplete.

## Glossary

- **Triage**: Classification of an incident or alert as noise, watch, or needs-attention.
- **RCA (Root Cause Analysis)**: An explanation of the most likely cause of an incident based on observed evidence.
- **Bedrock**: AWS Bedrock LLM service used as the reasoning layer.
- **Datadog MCP**: Model Context Protocol server exposing Datadog data (logs, traces, monitors, dashboards, incidents) to the analyzer agent.
- **Incident bundle**: A compact, compressed summary of evidence passed to Bedrock as part of the prompt.
- **LLM Observability**: Datadog feature for tracing LLM calls including input/output and cost metadata.
- **Confidence**: A value indicating how strongly the evidence supports a given conclusion.
- **Faulty workload**: A synthetic Kubernetes workload that emits controlled failures and anomalies for POC validation.
- **Trace ID**: A unique identifier propagated across logs, metrics, and traces to correlate telemetry for a single request.

## Requirements

### Requirement 1: Workload Telemetry Emission

**User Story:** As a platform engineer, I want the workload to emit realistic controlled failures and anomalies, so that the analyzer has representative telemetry to triage and analyze during the POC.

#### Acceptance Criteria

1. WHEN the workload runs, THE SYSTEM SHALL emit intermittent warnings, transient errors, latency spikes, and dependency timeout events.
2. WHEN the workload emits telemetry, THE SYSTEM SHALL record structured JSON fields for timestamp, service name, severity, request_id, error_type, message, and trace_id.
3. WHEN a request is processed, THE SYSTEM SHALL propagate trace_id across logs, metrics, and traces.
4. WHEN no request is actively being processed, THE SYSTEM SHALL allow partial trace_id propagation across available telemetry channels (e.g., metrics and traces without logs) without requiring additional configuration or permission flags.

### Requirement 2: Datadog as Sole Observability Backend

**User Story:** As a platform engineer, I want all telemetry stored in Datadog, so that the POC demonstrates a Datadog-native observability flow without dependency on CloudWatch.

#### Acceptance Criteria

1. WHEN logs, metrics, or traces are emitted, THE SYSTEM SHALL store them in Datadog.
2. WHEN the Datadog Agent is installed, THE SYSTEM SHALL collect logs, APM traces, and metrics from the cluster.
3. IF the Datadog Agent is installed but fails to collect due to configuration or permission issues, THE SYSTEM SHALL continue operating rather than treating the failure as a deployment blocker.
4. WHEN telemetry is stored, THE SYSTEM SHALL make it queryable in Datadog without requiring CloudWatch for the main POC flow.
5. WHEN the Datadog Agent is installed and operational, THE SYSTEM SHALL NOT require CloudWatch for telemetry collection or querying.

### Requirement 3: Datadog MCP Evidence Access

**User Story:** As an analyzer operator, I want the analyzer to retrieve incident evidence exclusively via Datadog MCP server calls, so that conclusions are grounded in real observed data.

#### Acceptance Criteria

1. WHEN the analyzer needs incident evidence, THE SYSTEM SHALL use Datadog MCP server calls exclusively, even when Datadog MCP is temporarily unavailable.
2. WHEN evidence is retrieved, THE SYSTEM SHALL use Datadog logs, traces, incidents, monitors, and dashboards as needed.
3. WHEN evidence is incomplete, THE SYSTEM SHALL state uncertainty instead of inventing missing details.

### Requirement 4: Incident Triage Classification

**User Story:** As a developer, I want the analyzer to classify incidents by operational importance, so that I can quickly distinguish actionable issues from noise.

#### Acceptance Criteria

1. WHEN an alert or incident is analyzed, THE SYSTEM SHALL classify it as one of: noise, watch, or needs-attention.
2. WHEN the available evidence is weak, THE SYSTEM SHALL evaluate the severity of any exception or error codes present before defaulting to a low-confidence or noise classification.
3. WHEN evidence is weak but error codes indicate a severe condition, THE SYSTEM SHALL escalate the classification to at least watch rather than dismissing it as noise.
4. WHEN the issue is significant, THE SYSTEM SHALL flag it for developer attention regardless of its triage classification.

### Requirement 5: Root Cause Analysis

**User Story:** As a developer, I want the analyzer to explain the most likely cause of an incident using observed evidence only, so that I can investigate with confidence in the direction provided.

#### Acceptance Criteria

1. WHEN RCA is generated, THE SYSTEM SHALL use Datadog MCP evidence as the primary source.
2. WHEN the evidence is weak, THE SYSTEM SHALL provide a tentative root cause analysis with a clearly marked low-confidence value rather than withholding the result entirely.
3. WHEN the analyzer returns RCA output, THE SYSTEM SHALL include the observed evidence, the likely root cause, and a confidence value.
4. WHEN some RCA components are unavailable, THE SYSTEM SHALL return partial results with whichever components are available, provided at least one of evidence, root cause, or confidence is present.
5. WHEN the analyzer cannot support any conclusion from evidence, THE SYSTEM SHALL explicitly state that no confident conclusion can be drawn.

### Requirement 6: Operational Recommendations

**User Story:** As a developer, I want the analyzer to provide operational focus areas rather than code fixes, so that I have a useful investigation direction without noise from speculative solutions.

#### Acceptance Criteria

1. WHEN recommendations are generated, THE SYSTEM SHALL include both root-cause direction and best-practice investigation areas in every recommendation.
2. WHEN the analyzer is uncertain, THE SYSTEM SHALL recommend the most relevant system area to inspect next.
3. WHEN recommendations are returned, THE SYSTEM SHALL default to operational focus areas and SHALL NOT include code-fix instructions unless the user explicitly requests them, regardless of the system's own assessment of the most relevant recommendation type.

### Requirement 7: Bedrock Reasoning Layer

**User Story:** As an analyzer operator, I want Bedrock to synthesize triage, RCA, and recommendations from the incident bundle, so that the output is structured and consistent.

#### Acceptance Criteria

1. WHEN an incident is selected, THE SYSTEM SHALL expose a POST /rca/analyze endpoint.
2. WHEN the analyzer receives incident metadata, THE SYSTEM SHALL accept incident_id, service name, time window, and optional namespace or pod filters.
3. WHEN the analyzer prepares the Bedrock request, THE SYSTEM SHALL compress the evidence into a compact incident bundle.
4. WHEN Bedrock returns a response, THE SYSTEM SHALL return JSON-only output containing triage_result, root_cause, evidence, recommended_focus, and confidence.

### Requirement 8: Agent Observability

**User Story:** As a platform engineer, I want the analyzer and its LLM workflow to be observable in Datadog, so that I can monitor and debug the agent's behavior end to end.

#### Acceptance Criteria

1. WHEN the analyzer calls Bedrock, THE SYSTEM SHALL instrument the call with Datadog LLM Observability.
2. WHEN the analyzer or agent runs, THE SYSTEM SHALL emit traces and logs into Datadog.
3. IF Datadog is unreachable during analyzer or agent execution, THE SYSTEM SHALL allow the analyzer or agent to abort its tasks gracefully but SHALL NOT cause a system-level failure or crash.
4. WHEN retries or failures occur, THE SYSTEM SHALL record the failure context in Datadog where supported.

### Requirement 9: Cost Control

**User Story:** As a POC owner, I want the system to minimize telemetry volume and prompt cost, so that the POC remains affordable and focused.

#### Acceptance Criteria

1. WHEN telemetry is logged, THE SYSTEM SHALL minimize volume and cardinality.
2. WHEN prompts are built, THE SYSTEM SHALL keep them short and evidence-focused.
3. WHEN raw logs are available, THE SYSTEM SHALL avoid sending high-volume dumps unless explicitly enabled.
4. WHEN high-volume log dumps are explicitly enabled, THE SYSTEM SHALL send them even if raw logs are currently available.
5. WHEN the POC runs without active analysis, THE SYSTEM SHALL continue operating even if Datadog instrumentation is reduced or disabled.
