# Design

## Architecture Overview
The system consists of four parts: a faulty workload pod, CloudWatch observability, an analyzer service, and a Datadog-instrumented Bedrock RCA step. The workload emits controlled failures and telemetry, CloudWatch stores the operational data, the analyzer gathers the relevant incident evidence, and Bedrock produces the summary while Datadog records the LLM workflow telemetry.

## Components
- Faulty workload pod: generates warnings, errors, latency spikes, and dependency timeouts.
- Telemetry pipeline: ships logs, metrics, and traces to CloudWatch.
- Analyzer service: exposes POST /rca/analyze and orchestrates incident retrieval.
- Datadog LLM Observability SDK: instruments the Bedrock analyzer path.
- Bedrock RCA step: receives a compact incident bundle and returns the RCA JSON.

## Data Flow
1. A request hits the workload pod.
2. The pod emits structured logs, metrics, and traces.
3. CloudWatch stores the telemetry.
4. An incident is detected or manually selected.
5. The analyzer queries the relevant CloudWatch window.
6. The analyzer summarizes the evidence into a compact bundle.
7. The analyzer sends the bundle to Bedrock through Datadog-instrumented code.
8. Datadog captures prompt, completion, latency, token, and error telemetry for the Bedrock call.
9. Bedrock returns the RCA summary.
10. The analyzer returns the result to the UI or caller.

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

## Datadog Observability Design
- Instrument the analyzer runtime with the Datadog Python SDK.
- Capture the Bedrock request as a traced LLM span.
- Preserve trace_id correlation between workload telemetry, CloudWatch evidence, and the analyzer request.
- Keep prompt and output capture controlled with masking or sampling rules.

## Cost Controls
- Log only errors, warnings, and selected sample requests.
- Keep metric labels low-cardinality.
- Use a short incident window for RCA.
- Summarize evidence before calling Bedrock.
- Avoid sending raw log dumps unless explicitly needed.
- Disable or reduce Datadog LLM payload capture if cost or privacy pressure increases.

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
- Datadog trace link for the LLM call

## Risks
- Excessive logging may increase cost.
- Too much raw telemetry may produce slow or noisy RCA.
- Poor trace correlation may reduce summary quality.
- Datadog prompt capture may expose sensitive data if not masked.

## Mitigations
- Enforce log sampling and retention limits.
- Summarize CloudWatch evidence before Bedrock invocation.
- Require trace_id on all request paths.
- Mask or truncate sensitive prompt content.
- Keep the first version to one service and one failure pattern.