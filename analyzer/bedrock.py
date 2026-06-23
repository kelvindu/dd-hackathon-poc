"""
Bedrock RCA integration.

Builds a compact prompt from an incident bundle and invokes the Claude model
via Amazon Bedrock to produce a JSON-only root-cause analysis.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger(__name__)

# Model and token settings — read from environment so they can be overridden
# without rebuilding the image.  Defaults match the original hardcoded values.
_MODEL_ID: str = os.environ.get(
    "BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0"
)
_MAX_TOKENS: int = int(os.environ.get("BEDROCK_MAX_TOKENS", "512"))

# Required keys that must be present in the Bedrock response
_REQUIRED_KEYS = {"root_cause", "evidence", "impact", "recommended_fix", "confidence"}


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


def invoke_rca(bundle: dict) -> dict:
    """
    Invoke the Claude model on Bedrock to produce a root-cause analysis.

    Sends the incident bundle as a compact prompt using the Anthropic Messages
    API format, parses the JSON response, and validates that all required keys
    are present.

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

    return rca
