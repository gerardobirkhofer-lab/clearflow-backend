#!/usr/bin/env bash
# Post-deploy health check for ClearFlow API (Railway).
# Run outside restricted sandboxes — needs open network to Railway.
set -euo pipefail

API_URL="${CLEARFLOW_API_URL:-https://clearflow-api-production.up.railway.app}"
EXPECTED_DEPLOY="${1:-}"
MAX_TRIES="${MAX_TRIES:-12}"
SLEEP_SECS="${SLEEP_SECS:-10}"

echo "Checking ${API_URL}/health (expect deploy contains: '${EXPECTED_DEPLOY:-any}')"

for i in $(seq 1 "$MAX_TRIES"); do
  body="$(curl -fsS --max-time 20 "${API_URL}/health" || true)"
  echo "try ${i}/${MAX_TRIES}: ${body:-"(no response)"}"
  if echo "$body" | grep -q '"status":"healthy"'; then
    if [[ -z "$EXPECTED_DEPLOY" ]] || echo "$body" | grep -q "$EXPECTED_DEPLOY"; then
      echo "OK"
      exit 0
    fi
  fi
  sleep "$SLEEP_SECS"
done

echo "FAIL: health check did not pass" >&2
exit 1
