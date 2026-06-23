#!/usr/bin/env bash
set -euo pipefail

WORKLOAD_URL="${WORKLOAD_URL:-http://localhost:8080}"
ANALYZER_URL="${ANALYZER_URL:-http://localhost:8000}"
REQUEST_COUNT="${SMOKE_REQUEST_COUNT:-50}"
WAIT_SECS="${SMOKE_INGESTION_WAIT_SECS:-90}"

echo "→ Sending $REQUEST_COUNT requests to faulty-workload..."
for i in $(seq 1 "$REQUEST_COUNT"); do
  curl -s "$WORKLOAD_URL/" > /dev/null
done

echo "→ Waiting ${WAIT_SECS}s for Datadog ingestion..."
sleep "$WAIT_SECS"

WINDOW_END=$(date -u +"%Y-%m-%dT%H:%M:%S")
WINDOW_START=$(date -u -v-5M +"%Y-%m-%dT%H:%M:%S" 2>/dev/null || \
               date -u -d '5 minutes ago' +"%Y-%m-%dT%H:%M:%S")

echo "→ Calling POST /rca/analyze ..."
RESPONSE=$(curl -sf -X POST "$ANALYZER_URL/rca/analyze" \
  -H "Content-Type: application/json" \
  -d "{
    \"incident_id\": \"smoke-test-001\",
    \"service\": \"faulty-workload\",
    \"window_start\": \"$WINDOW_START\",
    \"window_end\": \"$WINDOW_END\"
  }")

echo "$RESPONSE" | jq .

# Assertions
TRIAGE=$(echo "$RESPONSE" | jq -r '.triage_result')
CONFIDENCE=$(echo "$RESPONSE" | jq -r '.confidence')

if [[ "$TRIAGE" != "noise" && "$TRIAGE" != "watch" && "$TRIAGE" != "needs-attention" ]]; then
  echo "FAIL: triage_result='$TRIAGE' is not a valid value" >&2; exit 1
fi

if [[ -z "$(echo "$RESPONSE" | jq -r '.root_cause')" ]]; then
  echo "FAIL: root_cause is empty" >&2; exit 1
fi

if [[ -z "$(echo "$RESPONSE" | jq -r '.recommended_focus')" ]]; then
  echo "FAIL: recommended_focus is empty" >&2; exit 1
fi

echo "✓ triage_result=$TRIAGE  confidence=$CONFIDENCE"
echo "✓ Smoke test passed"
