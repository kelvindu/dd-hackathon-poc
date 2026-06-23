"""
Bedrock RCA integration.

Builds a compact prompt from an incident bundle and invokes the Claude model
via Amazon Bedrock to produce a JSON-only root-cause analysis.

Datadog LLM Observability is enabled when DD_LLMOBS_ENABLED=true is set in
the environment and the ddtrace package is installed.  When disabled or
unavailable the module behaves identically to the uninstrumented version.
"""

import json
import logging
import os

import boto3

# Optional Datadog LLM Observability — graceful degradation when not installed
try:
    from ddtrace.llmobs import LLMObs
except ImportError:
    LLMObs = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Model and token settings — read from environment so they can be overridden
# without rebuilding the image.  Defaults match the original hardcoded values.
_MODEL_ID: str = os.environ.get(
    "BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0"
)
_MAX_TOKENS: int = int(os.environ.get("BEDROCK_MAX_TOKENS", "512"))

# Required keys that must be present in the Bedrock response
_REQUIRED_KEYS = {"root_cause", "evidence", "impact", "recommended_fix", "confidence"}


# ---------------------------------------------------------------------------
# Datadog LLM Observability helpers
# ---------------------------------------------------------------------------

def _llmobs_enabled() -> bool:
    """Return True only when ddtrace is installed and DD_LLMOBS_ENABLED=true."""
    return LLMObs is not None and os.environ.get("DD_LLMOBS_ENABLED", "").lower() == "true"


_llmobs_initialized: bool = False


def _init_llmobs() -> None:
    """Initialise Datadog LLM Observability once at import time (no-op if disabled)."""
    global _llmobs_initialized
    if not _llmobs_enabled() or _llmobs_initialized:
        return
    try:
        LLMObs.enable(
            ml_app=os.environ.get("DD_LLMOBS_ML_APP", "cloudwatch-rca-poc"),
            agentless_enabled=os.environ.get("DD_LLMOBS_AGENTLESS_ENABLED", "true").lower() == "true",
            site=os.environ.get("DD_SITE", "datadoghq.com"),
            api_key=os.environ["DD_API_KEY"],
        )
        _llmobs_initialized = True
        logger.info(
            "Datadog LLM Observability enabled (ml_app=%s)",
            os.environ.get("DD_LLMOBS_ML_APP", "cloudwatch-rca-poc"),
        )
    except Exception as exc:
        logger.warning("Failed to initialise Datadog LLM Observability: %s", exc)


_init_llmobs()


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def build_prompt(bundle: dict) -> str:
    """
    Build a compact structured prompt from the incident bundle.

    The prompt instructs Claude to analyse the supplied incident data
    (log summary, metric deltas, trace anomalies) and return a JSON object
    with exactly five keys: root_cause, evidence, impact, recommended_fix,
    and confidence (0.0–1.0).

    Args:
        bundle: Incident bundle produced by ``build_bundle``. Expected keys:
                log_summary, metric_deltas, trace_anomalies.

    Returns:
        A prompt string targeting < 500 tokens.
    """
    log_summary = bundle.get("log_summary", [])
    metric_deltas = bundle.get("metric_deltas", {})
    trace_anomalies = bundle.get("trace_anomalies", [])

    incident_data = json.dumps(
        {
            "log_summary": log_summary,
            "metric_deltas": metric_deltas,
            "trace_anomalies": trace_anomalies,
        },
        separators=(",", ":"),  # compact representation to save tokens
    )

    prompt = (
        "You are an SRE assistant. Analyse the following Kubernetes incident data "
        "and return ONLY a JSON object with exactly these keys: "
        "root_cause (string), evidence (string), impact (string), "
        "recommended_fix (string), confidence (float 0.0-1.0). "
        "No explanation outside the JSON.\n\n"
        f"Incident data: {incident_data}"
    )

    return prompt


# ---------------------------------------------------------------------------
# Bedrock invocation
# ---------------------------------------------------------------------------

def invoke_rca(bundle: dict) -> dict:
    """
    Invoke the Claude model on Bedrock to produce a root-cause analysis.

    Sends the incident bundle as a compact prompt using the Anthropic Messages
    API format, parses the JSON response, and validates that all required keys
    are present.

    When Datadog LLM Observability is enabled the call is wrapped in an LLM
    span that records the prompt, completion, approximate token counts, and any
    error metadata.  Errors always propagate to the caller so the API returns
    a 502 regardless of instrumentation state.

    Args:
        bundle: Incident bundle produced by ``build_bundle``.

    Returns:
        A dict containing: root_cause, evidence, impact, recommended_fix,
        confidence.

    Raises:
        ValueError: If the response JSON is missing any of the required keys.
        json.JSONDecodeError: If the response body cannot be parsed as JSON.
    """
    prompt = build_prompt(bundle)

    # Approximate token count: ~1 token per 4 characters
    approx_tokens = len(prompt) // 4
    logger.info("Bedrock prompt approximate token count: %d", approx_tokens)
    if approx_tokens > 500:
        logger.warning(
            "Bedrock prompt exceeds 500-token target (approx %d tokens); "
            "consider trimming the incident bundle to reduce cost.",
            approx_tokens,
        )

    # Open a Datadog LLM span when observability is active, otherwise span=None
    # and every span-related branch below is skipped (graceful degradation).
    span = None
    if _llmobs_enabled():
        span = LLMObs.llm(
            model_name=_MODEL_ID,
            model_provider="bedrock",
            name="invoke_rca",
        )
        span.__enter__()
        LLMObs.annotate(span, input_data=[{"role": "user", "content": prompt}])

    try:
        client = boto3.client("bedrock-runtime")

        request_body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": _MAX_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        }

        response = client.invoke_model(
            modelId=_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(request_body),
        )

        response_body = json.loads(response["body"].read())

        # Extract the text content from the Anthropic Messages API response
        # Response format: {"content": [{"type": "text", "text": "..."}], ...}
        content_blocks = response_body.get("content", [])
        text_content = ""
        for block in content_blocks:
            if block.get("type") == "text":
                text_content = block["text"].strip()
                break

        rca = json.loads(text_content)

        # Validate all required keys are present
        missing = _REQUIRED_KEYS - set(rca.keys())
        if missing:
            raise ValueError(
                f"Bedrock response missing required keys: {sorted(missing)}. "
                f"Got: {sorted(rca.keys())}"
            )

        if span:
            LLMObs.annotate(
                span,
                output_data=[{"role": "assistant", "content": text_content}],
                metadata={
                    "input_tokens": approx_tokens,
                    "output_tokens": len(text_content) // 4,
                },
            )
            span.__exit__(None, None, None)

        return rca

    except Exception as exc:
        if span:
            LLMObs.annotate(span, metadata={"error": str(exc)})
            span.__exit__(type(exc), exc, exc.__traceback__)
        raise
