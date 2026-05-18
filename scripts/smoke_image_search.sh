#!/usr/bin/env bash
# Smoke test: image search via POST /api/recommendation/search/image
# Override the test image: IMAGE_PATH=/path/to/image.jpg make smoke-image
set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
K="${K:-5}"
ENDPOINT="${GATEWAY_URL}/api/recommendation/search/image"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_IMAGE="${REPO_ROOT}/data-pipeline/data/raw/images/010/0108775015.jpg"
IMAGE_PATH="${IMAGE_PATH:-${DEFAULT_IMAGE}}"

echo "==> smoke-image"
echo "    endpoint   : ${ENDPOINT}"
echo "    image_path : ${IMAGE_PATH}"
echo "    k          : ${K}"
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
  --arg mode "image_to_image" \
  --arg image_base64 "${IMAGE_B64}" \
  --argjson k "${K}" \
  '{"mode": $mode, "image_base64": $image_base64, "k": $k}')

HTTP_CODE=$(curl -s -o /tmp/ss_smoke_image.json -w "%{http_code}" \
  --max-time 20 \
  -X POST "${ENDPOINT}" \
  -H "Content-Type: application/json" \
  -d "${PAYLOAD}")
BODY=$(cat /tmp/ss_smoke_image.json)
echo "${BODY}" | jq .
echo ""

[[ "${HTTP_CODE}" == "200" ]] || { echo "FAIL: HTTP ${HTTP_CODE}" >&2; exit 1; }

API_CODE=$(echo "${BODY}" | jq '.code')
[[ "${API_CODE}" == "200" ]] || { echo "FAIL: .code=${API_CODE}" >&2; exit 1; }

ITEM_COUNT=$(echo "${BODY}" | jq '.data.items | length')
[[ "${ITEM_COUNT}" -gt 0 ]] || { echo "FAIL: results empty" >&2; exit 1; }

DEGRADED=$(echo "${BODY}" | jq '.data.degraded')
[[ "${DEGRADED}" == "false" ]] || { echo "FAIL: degraded=${DEGRADED}" >&2; exit 1; }

FIRST_ID=$(echo "${BODY}" | jq -r '.data.items[0].itemId')
ID_LEN=${#FIRST_ID}
[[ "${ID_LEN}" == "10" ]] || { echo "WARN: itemId '${FIRST_ID}' is ${ID_LEN} chars (expected 10)"; }

MODE=$(echo "${BODY}" | jq -r '.data.mode')

echo "PASS smoke-image | items=${ITEM_COUNT} degraded=${DEGRADED} mode=${MODE} itemId=${FIRST_ID}"
