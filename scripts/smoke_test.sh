#!/usr/bin/env bash
# smoke_test.sh — End-to-end validation of the faulty-workload → CloudWatch → RCA pipeline.
#
# Usage:
#   WORKLOAD_URL=http://localhost:8080 ANALYZER_URL=http://localhost:8000 ./scripts/smoke_test.sh
#
# Requirements:
#   curl, jq
#
# Exit codes:
#   0 — all assertions passed
#   1 — a dependency is missing, a curl call failed, or an assertion failed

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WORKLOAD_URL="${WORKLOAD_URL:-http://localhost:8080}"
ANALYZER_URL="${ANALYZER_URL:-http://localhost:8000}"
REQUEST_COUNT=50
INGESTION_WAIT_SECS=60

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

check_dep() {
    if ! command -v "$1" &>/dev/null; then
        echo "ERROR: required tool '$1' is not installed or not on PATH." >&2
        exit 1
    fi
}

check_dep curl
check_dep jq

# ---------------------------------------------------------------------------
# Helper: ISO-8601 UTC timestamp
# On macOS, date -u +%Y-%m-%dT%H:%M:%SZ works out of the box.
# On Linux (GNU coreutils), the same format is also valid.
# ---------------------------------------------------------------------------

utc_now() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

# ---------------------------------------------------------------------------
# Step 1: Record the start of the test window
# ---------------------------------------------------------------------------

echo "==> Recording window_start..."
WINDOW_START="$(utc_now)"
echo "    window_start = ${WINDOW_START}"

# ---------------------------------------------------------------------------
# Step 2: Send 50 GET / requests to the faulty workload
# Some will return HTTP 500 intentionally — that is expected behaviour.
# ---------------------------------------------------------------------------

echo "==> Sending ${REQUEST_COUNT} requests to ${WORKLOAD_URL} ..."
for i in $(seq 1 "${REQUEST_COUNT}"); do
    # --fail-with-body would stop on 4xx/5xx; we use --silent --output /dev/null
    # so that 500 responses are silently consumed.
    curl --silent \
         --output /dev/null \
         --max-time 10 \
         "${WORKLOAD_URL}/" || true
done

echo "    Sent ${REQUEST_COUNT} requests. Waiting ${INGESTION_WAIT_SECS}s for CloudWatch ingestion..."

# ---------------------------------------------------------------------------
# Step 3: Wait for CloudWatch to ingest the telemetry
# ---------------------------------------------------------------------------

sleep "${INGESTION_WAIT_SECS}"

# ---------------------------------------------------------------------------
# Step 4: Record the end of the test window
# ---------------------------------------------------------------------------

WINDOW_END="$(utc_now)"
echo "    window_end   = ${WINDOW_END}"

# ---------------------------------------------------------------------------
# Step 5: Build the POST /rca/analyze request body
# ---------------------------------------------------------------------------

REQUEST_BODY="$(jq -n \
    --arg incident_id  "smoke-test-001" \
    --arg service      "faulty-workload" \
    --arg window_start "${WINDOW_START}" \
    --arg window_end   "${WINDOW_END}" \
    '{
        incident_id:  $incident_id,
        service:      $service,
        window_start: $window_start,
        window_end:   $window_end
    }'
)"

echo "==> Calling POST ${ANALYZER_URL}/rca/analyze ..."
echo "    Request body: ${REQUEST_BODY}"

# ---------------------------------------------------------------------------
# Step 6: Call the analyzer and capture the response
# ---------------------------------------------------------------------------

HTTP_RESPONSE="$(curl --silent \
    --write-out "\n%{http_code}" \
    --request POST \
    --header "Content-Type: application/json" \
    --data "${REQUEST_BODY}" \
    --max-time 120 \
    "${ANALYZER_URL}/rca/analyze")"

# Split response body and HTTP status code (last line)
HTTP_BODY="$(echo "${HTTP_RESPONSE}" | head -n -1)"
HTTP_STATUS="$(echo "${HTTP_RESPONSE}" | tail -n 1)"

echo "    HTTP status: ${HTTP_STATUS}"
echo "    Response body:"
echo "${HTTP_BODY}" | jq . 2>/dev/null || echo "${HTTP_BODY}"

# ---------------------------------------------------------------------------
# Step 7: Validate HTTP status is 200
# ---------------------------------------------------------------------------

if [[ "${HTTP_STATUS}" != "200" ]]; then
    echo "ERROR: Expected HTTP 200 from /rca/analyze, got ${HTTP_STATUS}." >&2
    echo "       Response: ${HTTP_BODY}" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 8: Validate all five required RCA fields are present
# ---------------------------------------------------------------------------

REQUIRED_FIELDS=("root_cause" "evidence" "impact" "recommended_fix" "confidence")
PASS=true

for field in "${REQUIRED_FIELDS[@]}"; do
    value="$(echo "${HTTP_BODY}" | jq -r --arg f "${field}" '.[$f] // empty')"
    if [[ -z "${value}" ]]; then
        echo "ERROR: Required field '${field}' is missing or null in the RCA response." >&2
        PASS=false
    else
        echo "    [OK] '${field}' is present."
    fi
done

if [[ "${PASS}" == "false" ]]; then
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 9: Validate confidence > 0
# ---------------------------------------------------------------------------

CONFIDENCE="$(echo "${HTTP_BODY}" | jq -r '.confidence')"

# jq comparison: returns 1 (true in jq terms) when confidence > 0
CONFIDENCE_OK="$(echo "${HTTP_BODY}" | jq '.confidence > 0')"

if [[ "${CONFIDENCE_OK}" != "true" ]]; then
    echo "ERROR: Expected confidence > 0, got '${CONFIDENCE}'." >&2
    exit 1
fi

echo "    [OK] confidence = ${CONFIDENCE} (> 0)"

# ---------------------------------------------------------------------------
# All checks passed
# ---------------------------------------------------------------------------

echo ""
echo "==> Smoke test PASSED. All five RCA fields present and confidence > 0."
exit 0
