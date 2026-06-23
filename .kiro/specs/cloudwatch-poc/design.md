# Design

## Architecture Overview
The system consists of four parts: a faulty workload pod, CloudWatch observability, an analyzer service, and Bedrock-based RCA generation. The workload emits controlled failures and telemetry, CloudWatch stores the operational data, the analyzer gathers the relevant incident evidence, and Bedrock produces the summary.

## Components
- Faulty workload pod: generates warnings, errors, latency spikes, and dependency timeouts.
- Telemetry pipeline: ships logs, metrics, and traces to CloudWatch.
- Analyzer service: exposes POST /rca/analyze and orchestrates incident retrieval.
- Bedrock RCA step: receives a compact incident bundle and returns the RCA JSON.

## Data Flow
1. A request hits the workload pod.
2. The pod emits structured logs, metrics, and traces.
3. CloudWatch stores the telemetry.
4. An incident is detected or manually selected.
5. The analyzer queries the relevant CloudWatch window.
6. The analyzer summarizes the evidence into a compact bundle.
7. The analyzer sends the bundle to Bedrock.
8. Bedrock returns the RCA summary.
9. The analyzer returns the result to the UI or caller.

## Interface Design
### POST /rca/analyze
Request fields:
- incident_id
- service
- window_start
- window_end
- namespace
- pod_name

Response fields:
- root_cause
- evidence
- impact
- recommended_fix
- confidence

## Observability Design
- Use structured JSON logs with trace_id and request_id.
- Keep metrics limited to the essential pod and incident signals.
- Use CloudWatch as the primary observability store.
- Use short time windows for retrieval to reduce query cost.

## Cost Controls
- Log only errors, warnings, and selected sample requests.
- Keep metric labels low-cardinality.
- Use a small incident window for RCA.
- Summarize evidence before calling Bedrock.
- Avoid sending raw log dumps unless explicitly needed.

## RCA Prompt Strategy
The analyzer should ask Bedrock for:
- primary root cause
- supporting evidence
- impact
- likely fix
- confidence score

The prompt should request JSON-only output to simplify parsing and display.

## UI Considerations
The interface can be a simple incident view with:
- incident selector
- evidence timeline
- CloudWatch log and trace excerpts
- Bedrock RCA output card

## Risks
- Excessive logging may increase cost.
- Too much raw telemetry may produce slow or noisy RCA.
- Poor trace correlation may reduce summary quality.

## Mitigations
- Enforce log sampling and retention limits.
- Summarize CloudWatch evidence before Bedrock invocation.
- Require trace_id on all request paths.
- Keep the first version to one service and one failure pattern.