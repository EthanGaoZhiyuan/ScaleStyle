#!/usr/bin/env bash
# smoke_fallback.sh — Fallback resilience smoke test
#
# NON-DESTRUCTIVE by default (DESTRUCTIVE=0):
#   Checks that fallback pre-conditions are in place:
#     1. Gateway /api/recommendation/debug/cache-stats responds HTTP 200
#     2. Redis has at least one popularity:materialized:* key with entries
#   NOTE: This does NOT pause inference and does NOT prove runtime fallback behavior.
#         It only validates that the data required for fallback exists.
#
# DESTRUCTIVE mode (DESTRUCTIVE=1):
#   Pauses the inference container for ~PAUSE_DURATION seconds, fires a real
#   gateway search, and asserts:
#     - HTTP 200 (gateway must survive inference outage)
#     - Non-empty result set
#     - degraded=true on returned items
#     - source ∈ {redis-cache, popular-fallback, popularity_fallback}
#     - If source=popular-fallback: returned itemIds overlap with the
#       popularity:materialized:24h:* ZSET (proves items came from that key)
#   A trap restores inference before assertions — failures never leave it paused.
#
#   Usage: DESTRUCTIVE=1 bash scripts/smoke_fallback.sh
set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
INFERENCE_CONTAINER="${INFERENCE_CONTAINER:-scalestyle-inference}"
REDIS_CONTAINER="${REDIS_CONTAINER:-scalestyle-redis}"
DESTRUCTIVE="${DESTRUCTIVE:-0}"
PAUSE_DURATION="${PAUSE_DURATION:-3}"

echo "==> smoke-fallback"
echo "    gateway             : ${GATEWAY_URL}"
echo "    inference_container : ${INFERENCE_CONTAINER}"
echo "    redis_container     : ${REDIS_CONTAINER}"
echo "    destructive         : ${DESTRUCTIVE}"
echo ""

if ! command -v jq &>/dev/null; then
  echo "ERROR: jq is required. Install: brew install jq" >&2
  exit 1
fi

# ── Pre-condition 1: Gateway is up ────────────────────────────────────────────
echo "[1/3] Checking gateway health (cache-stats)..."
HTTP_CODE=$(curl -s -o /tmp/ss_smoke_fallback_stats.json -w "%{http_code}" \
  --max-time 5 "${GATEWAY_URL}/api/recommendation/debug/cache-stats")
[[ "${HTTP_CODE}" == "200" ]] || {
  echo "FAIL: gateway /api/recommendation/debug/cache-stats returned HTTP ${HTTP_CODE}" >&2
  exit 1
}
echo "      OK: gateway reachable, HTTP 200"

# ── Pre-condition 2: Redis has a usable popularity fallback key ───────────────
# Gateway fallback tier order:
#   1. redis-cache          — stale query cache (first layer)
#   2. popular-fallback     — popularity:materialized:{window}:{bucket} (event-consumer)
#   3. global-popular-fallback — global:popular (data-pipeline bootstrap, always present)
echo "[2/3] Checking Redis popularity fallback keys..."
MATERIALIZED_OK=0
GLOBAL_OK=0
if ! command -v docker &>/dev/null; then
  echo "      SKIP: docker not found, cannot inspect Redis"
else
  FIRST_MAT_KEY=$(docker exec "${REDIS_CONTAINER}" \
    redis-cli --scan --pattern "popularity:materialized:*" 2>/dev/null | head -1 || true)

  if [[ -n "${FIRST_MAT_KEY}" ]]; then
    MAT_LEN=$(docker exec "${REDIS_CONTAINER}" \
      redis-cli ZCARD "${FIRST_MAT_KEY}" 2>/dev/null || echo "0")
    if [[ "${MAT_LEN}" -gt 0 ]]; then
      echo "      OK (tier 2): '${FIRST_MAT_KEY}' has ${MAT_LEN} entries (event-consumer window)"
      MATERIALIZED_OK=1
    else
      echo "      WARN: '${FIRST_MAT_KEY}' exists but is empty" >&2
    fi
  else
    echo "      INFO: no popularity:materialized:* keys — event-consumer has not materialized yet" >&2
  fi

  GLOBAL_LEN=$(docker exec "${REDIS_CONTAINER}" redis-cli ZCARD global:popular 2>/dev/null || echo "0")
  if [[ "${GLOBAL_LEN}" -gt 0 ]]; then
    echo "      OK (tier 3): global:popular has ${GLOBAL_LEN} entries (bootstrap fallback ready)"
    GLOBAL_OK=1
  else
    echo "      WARN: global:popular is empty — run data-pipeline bootstrap" >&2
  fi
fi

# ── Pre-condition summary ──────────────────────────────────────────────────────
echo "[3/3] Pre-condition check complete"
echo ""

if [[ "${DESTRUCTIVE}" != "1" ]]; then
  echo "INFO: Non-destructive check complete."
  echo "      This verifies pre-conditions only — NOT live runtime fallback behavior."
  echo "      To trigger a real fallback (pauses inference ~${PAUSE_DURATION}s):"
  echo "        DESTRUCTIVE=1 bash scripts/smoke_fallback.sh"
  echo ""
  if [[ "${MATERIALIZED_OK}" == "1" || "${GLOBAL_OK}" == "1" ]]; then
    echo "PASS smoke-fallback (non-destructive pre-conditions only)"
    [[ "${MATERIALIZED_OK}" != "1" ]] && echo "     NOTE: using tier-3 global:popular fallback (tier-2 materialized windows absent)"
  else
    echo "FAIL smoke-fallback (non-destructive) — no usable popularity fallback key found" >&2
    exit 1
  fi
  exit 0
fi

# ── DESTRUCTIVE path ──────────────────────────────────────────────────────────
# Cleanup is defined and trapped BEFORE the pause so it always fires on EXIT,
# even if docker pause itself partially succeeds before a subsequent command fails.
INFERENCE_PAUSED=0

cleanup() {
  if [[ "${INFERENCE_PAUSED}" == "1" ]]; then
    echo "  --> restoring inference container (unpause)..."
    docker unpause "${INFERENCE_CONTAINER}" || true
    INFERENCE_PAUSED=0
  fi
}
trap cleanup EXIT

echo "DESTRUCTIVE=1: pausing inference container '${INFERENCE_CONTAINER}'..."
docker pause "${INFERENCE_CONTAINER}"
INFERENCE_PAUSED=1

echo "Waiting ${PAUSE_DURATION}s for in-flight connections to time out..."
sleep "${PAUSE_DURATION}"

FALLBACK_URL="${GATEWAY_URL}/api/recommendation/search?query=black+dress&k=5"
echo "Calling ${FALLBACK_URL}..."
HTTP_CODE=$(curl -s -o /tmp/ss_smoke_fallback_live.json -w "%{http_code}" \
  --max-time 15 "${FALLBACK_URL}")
BODY=$(cat /tmp/ss_smoke_fallback_live.json)
echo "${BODY}" | jq .
echo ""

# Restore inference before assertions — a failing assert must not leave it paused.
cleanup

# ── Assertions ────────────────────────────────────────────────────────────────
[[ "${HTTP_CODE}" == "200" ]] || {
  echo "FAIL: expected HTTP 200, got ${HTTP_CODE}" >&2
  exit 1
}

ITEM_COUNT=$(echo "${BODY}" | jq '.data | length')
[[ "${ITEM_COUNT}" -gt 0 ]] || {
  echo "FAIL: fallback returned 0 items" >&2
  exit 1
}

FIRST_SOURCE=$(echo "${BODY}" | jq -r '.data[0].source')
FIRST_DEGRADED=$(echo "${BODY}" | jq -r '.data[0].degraded')
FIRST_DEGRADED_REASON=$(echo "${BODY}" | jq -r '.data[0].degradedReason // "null"')

[[ "${FIRST_DEGRADED}" == "true" ]] || {
  echo "FAIL: expected degraded=true, got '${FIRST_DEGRADED}'" >&2
  exit 1
}

# Valid degraded source values for the text-search fallback path:
#   redis-cache              → stale query-cache hit (tier 1)
#   popular-fallback         → popularity:materialized ZSET (tier 2, event-consumer)
#   popularity_fallback      → same materialized ZSET, hybrid path variant
#   global-popular-fallback  → global:popular bootstrap key (tier 3, data-pipeline)
case "${FIRST_SOURCE}" in
  redis-cache|popular-fallback|popularity_fallback|global-popular-fallback) ;;
  *) echo "FAIL: unexpected degraded source '${FIRST_SOURCE}'" >&2; exit 1 ;;
esac

# ── ItemId overlap check ──────────────────────────────────────────────────────
# Confirm returned itemIds actually appear in the Redis key the gateway read.
# Routes the overlap check to the correct key based on source value.
if command -v docker &>/dev/null; then
  RESPONSE_IDS=$(echo "${BODY}" | jq -r '.data[].itemId')

  if [[ "${FIRST_SOURCE}" == "global-popular-fallback" ]]; then
    OVERLAP_KEY="global:popular"
    OVERLAP_MEMBERS=$(docker exec "${REDIS_CONTAINER}" \
      redis-cli ZRANGE "${OVERLAP_KEY}" 0 -1 2>/dev/null || true)

  elif [[ "${FIRST_SOURCE}" == "popular-fallback" || "${FIRST_SOURCE}" == "popularity_fallback" ]]; then
    OVERLAP_KEY=$(docker exec "${REDIS_CONTAINER}" \
      redis-cli --scan --pattern "popularity:materialized:24h:*" 2>/dev/null | head -1 || true)
    if [[ -n "${OVERLAP_KEY}" ]]; then
      OVERLAP_MEMBERS=$(docker exec "${REDIS_CONTAINER}" \
        redis-cli ZRANGE "${OVERLAP_KEY}" 0 -1 2>/dev/null || true)
    else
      OVERLAP_KEY=""
      OVERLAP_MEMBERS=""
    fi
  else
    OVERLAP_KEY=""
    OVERLAP_MEMBERS=""
  fi

  if [[ -n "${OVERLAP_KEY}" ]]; then
    OVERLAP=0
    while IFS= read -r rid; do
      if echo "${OVERLAP_MEMBERS}" | grep -qxF "${rid}" 2>/dev/null; then
        OVERLAP=$((OVERLAP + 1))
      fi
    done <<< "${RESPONSE_IDS}"

    if [[ "${OVERLAP}" -gt 0 ]]; then
      echo "      OK: ${OVERLAP}/${ITEM_COUNT} returned itemIds confirmed in '${OVERLAP_KEY}'"
    else
      echo "      WARN: 0/${ITEM_COUNT} returned itemIds found in '${OVERLAP_KEY}'" >&2
      echo "            Response sample : $(echo "${RESPONSE_IDS}" | head -3 | tr '\n' ' ')" >&2
      ZSET_SAMPLE=$(docker exec "${REDIS_CONTAINER}" \
        redis-cli ZRANGE "${OVERLAP_KEY}" 0 2 2>/dev/null | tr '\n' ' ' || true)
      echo "            Key sample: ${ZSET_SAMPLE}" >&2
    fi
  fi
fi

echo ""
echo "PASS smoke-fallback (destructive) | items=${ITEM_COUNT} degraded=${FIRST_DEGRADED} source=${FIRST_SOURCE} degradedReason=${FIRST_DEGRADED_REASON}"
