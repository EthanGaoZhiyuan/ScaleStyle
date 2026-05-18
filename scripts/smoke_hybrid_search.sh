#!/usr/bin/env bash
# Smoke test: hybrid text+image search via POST /api/recommendation/search/hybrid
# Override the test image: IMAGE_PATH=/path/to/image.jpg make smoke-hybrid
set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
QUERY="${QUERY:-similar but black}"
K="${K:-5}"
IMAGE_WEIGHT="${IMAGE_WEIGHT:-0.5}"
TEXT_WEIGHT="${TEXT_WEIGHT:-0.4}"
BEHAVIOR_WEIGHT="${BEHAVIOR_WEIGHT:-0.1}"
ENDPOINT="${GATEWAY_URL}/api/recommendation/search/hybrid"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_IMAGE="${REPO_ROOT}/data-pipeline/data/raw/images/010/0108775015.jpg"
IMAGE_PATH="${IMAGE_PATH:-${DEFAULT_IMAGE}}"

echo "==> smoke-hybrid"
echo "    endpoint   : ${ENDPOINT}"
echo "    image_path : ${IMAGE_PATH}"
echo "    query      : ${QUERY}"
echo "    k          : ${K}"
echo "    weights    : image=${IMAGE_WEIGHT} text=${TEXT_WEIGHT} behavior=${BEHAVIOR_WEIGHT}"
echo ""

if ! command -v jq &>/dev/null; then
  echo "ERROR: jq is required. Install: brew install jq" >&2
  exit 1
fi

if [[ ! -f "${IMAGE_PATH}" ]]; then
  echo "ERROR: image not found: ${IMAGE_PATH}" >&2
  echo "       Set IMAGE_PATH env var to override." >&2
  exit 1
fi

IMAGE_B64=$(base64 -i "${IMAGE_PATH}" 2>/dev/null | tr -d '\n' \
  || base64 "${IMAGE_PATH}" | tr -d '\n')

PAYLOAD=$(jq -n \
  --arg query "${QUERY}" \
  --arg image_base64 "${IMAGE_B64}" \
  --argjson k "${K}" \
  --argjson image_weight "${IMAGE_WEIGHT}" \
  --argjson text_weight "${TEXT_WEIGHT}" \
  --argjson behavior_weight "${BEHAVIOR_WEIGHT}" \
  '{
    "query": $query,
    "image_base64": $image_base64,
    "k": $k,
    "image_weight": $image_weight,
    "text_weight": $text_weight,
    "behavior_weight": $behavior_weight
  }')

HTTP_CODE=$(curl -s -o /tmp/ss_smoke_hybrid.json -w "%{http_code}" \
  --max-time 20 \
  -X POST "${ENDPOINT}" \
  -H "Content-Type: application/json" \
  -d "${PAYLOAD}")
BODY=$(cat /tmp/ss_smoke_hybrid.json)
echo "${BODY}" | jq .
echo ""

[[ "${HTTP_CODE}" == "200" ]] || { echo "FAIL: HTTP ${HTTP_CODE}" >&2; exit 1; }

API_CODE=$(echo "${BODY}" | jq '.code')
[[ "${API_CODE}" == "200" ]] || { echo "FAIL: .code=${API_CODE}" >&2; exit 1; }

ITEM_COUNT=$(echo "${BODY}" | jq '.data.items | length')
[[ "${ITEM_COUNT}" -gt 0 ]] || { echo "FAIL: results empty" >&2; exit 1; }

DEGRADED=$(echo "${BODY}" | jq '.data.degraded')
[[ "${DEGRADED}" == "false" ]] || { echo "FAIL: degraded=${DEGRADED}" >&2; exit 1; }

MODE=$(echo "${BODY}" | jq -r '.data.mode')
[[ "${MODE}" == "hybrid" ]] || { echo "FAIL: mode=${MODE} (expected hybrid)" >&2; exit 1; }

ARCH=$(echo "${BODY}" | jq -r '.data.architecture')

FIRST_ID=$(echo "${BODY}" | jq -r '.data.items[0].itemId')
ID_LEN=${#FIRST_ID}
[[ "${ID_LEN}" == "10" ]] || { echo "WARN: itemId '${FIRST_ID}' is ${ID_LEN} chars (expected 10)"; }

FINAL_SCORE=$(echo "${BODY}" | jq '.data.items[0].finalScore')
[[ "${FINAL_SCORE}" != "null" ]] || { echo "FAIL: finalScore is null" >&2; exit 1; }
FINAL_SCORE_VALID=$(echo "${FINAL_SCORE} > 0" | bc -l 2>/dev/null || echo "1")
[[ "${FINAL_SCORE_VALID}" == "1" ]] || { echo "WARN: finalScore=${FINAL_SCORE} (not positive)"; }

CANDIDATE_SOURCES=$(echo "${BODY}" | jq -r '.data.items[0].candidateSources | join(",")')

echo "PASS smoke-hybrid | items=${ITEM_COUNT} degraded=${DEGRADED} mode=${MODE} arch=${ARCH} finalScore=${FINAL_SCORE} sources=${CANDIDATE_SOURCES} itemId=${FIRST_ID}"
