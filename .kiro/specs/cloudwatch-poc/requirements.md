# Requirements

## Goal
Build a proof-of-concept observability pipeline for Kubernetes pods running on EC2 that intentionally emits intermittent warnings and errors, stores telemetry in CloudWatch, and uses Amazon Bedrock to generate a concise root-cause analysis summary.

## User Stories
- As an operator, I want pods to emit controlled warnings and errors so that I can validate observability and RCA behavior.
- As an operator, I want pod logs, metrics, and traces to be visible in CloudWatch so that I can inspect incidents.
- As an operator, I want an analyzer service to summarize the incident using Bedrock so that I can get a root-cause analysis quickly.
- As an operator, I want the solution to remain low cost so that it fits a small proof of concept budget.

## Functional Requirements
- The workload shall generate intermittent warnings, transient errors, latency spikes, and dependency timeout events.
- The workload shall emit structured JSON logs containing timestamp, service name, severity, trace_id, request_id, error_type, and message.
- The workload shall expose basic metrics including request_count, warning_count, error_count, latency_ms, timeout_count, and restart_count.
- The workload shall propagate a trace_id across logs, metrics, and traces.
- The telemetry shall be observable in CloudWatch.
- The analyzer service shall expose a POST /rca/analyze endpoint.
- The analyzer service shall accept incident metadata including incident_id, service name, time window, and optional namespace or pod filters.
- The analyzer service shall query CloudWatch for the relevant incident window.
- The analyzer service shall compress the observed data into a compact incident bundle before sending it to Bedrock.
- Bedrock shall return a JSON-only RCA response containing root_cause, evidence, impact, recommended_fix, and confidence.

## Non-Functional Requirements
- The solution shall be suitable for a low-budget proof of concept.
- The solution shall minimize log volume and metric cardinality.
- The solution shall avoid sending raw high-volume logs to Bedrock unless explicitly enabled.
- The solution shall keep prompts short to reduce cost and latency.
- The solution shall support a small EC2-hosted Kubernetes environment.

## Acceptance Criteria
- A pod can emit warnings and errors on demand.
- CloudWatch shows the generated events and metrics.
- The analyzer can retrieve the incident evidence from CloudWatch.
- Bedrock returns a structured RCA summary.
- The solution stays within POC cost limits.