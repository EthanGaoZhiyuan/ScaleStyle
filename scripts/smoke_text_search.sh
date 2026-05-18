#!/usr/bin/env bash
# Smoke test: text search via GET /api/recommendation/search
set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
QUERY="${QUERY:-black dress}"
K="${K:-5}"
ENDPOINT="${GATEWAY_URL}/api/recommendation/search"

echo "==> smoke-text"
echo "    endpoint : ${ENDPOINT}"
echo "    query    : ${QUERY}"
echo "    k        : ${K}"
echo ""

if ! command -v jq &>/dev/null; then
  echo "ERROR: jq is required. Install: brew install jq" >&2
  exit 1
fi

ENCODED_QUERY="${QUERY// /+}"
URL="${ENDPOINT}?query=${ENCODED_QUERY}&k=${K}"

HTTP_CODE=$(curl -s -o /tmp/ss_smoke_text.json -w "%{http_code}" --max-time 15 "${URL}")
BODY=$(cat /tmp/ss_smoke_text.json)
echo "${BODY}" | jq .
echo ""

[[ "${HTTP_CODE}" == "200" ]] || { echo "FAIL: HTTP ${HTTP_CODE}" >&2; exit 1; }

API_CODE=$(echo "${BODY}" | jq '.code')
[[ "${API_CODE}" == "200" ]] || { echo "FAIL: .code=${API_CODE}" >&2; exit 1; }

ITEM_COUNT=$(echo "${BODY}" | jq '.data | length')
[[ "${ITEM_COUNT}" -gt 0 ]] || { echo "FAIL: results empty" >&2; exit 1; }

FIRST_DEGRADED=$(echo "${BODY}" | jq '.data[0].degraded')
[[ "${FIRST_DEGRADED}" == "false" ]] || { echo "FAIL: degraded=${FIRST_DEGRADED}" >&2; exit 1; }

FIRST_ID=$(echo "${BODY}" | jq -r '.data[0].itemId')
ID_LEN=${#FIRST_ID}
[[ "${ID_LEN}" == "10" ]] || { echo "WARN: itemId '${FIRST_ID}' is ${ID_LEN} chars (expected 10)"; }

echo "PASS smoke-text | items=${ITEM_COUNT} degraded=${FIRST_DEGRADED} itemId=${FIRST_ID}"
