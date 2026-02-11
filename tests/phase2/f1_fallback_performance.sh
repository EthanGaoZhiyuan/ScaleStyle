#!/bin/bash

################################################################################
# Phase 2 Test Suite - F1. Fallback-only Performance Test
#
# Tests pure fallback latency in degraded mode after circuit breaker fully open
#
# v2.2 Standard:
#   - p99(degraded) < 20ms
#   - degraded_ratio ≥ 0.99
#   - error_rate ≤ 0.1%
#
# Exit Code: 0 if pass, 1 if fail
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

NAMESPACE="${NAMESPACE:-scalestyle}"
RESULT_FILE="$TEST_OUTPUT_DIR/f1_fallback_performance_${TEST_TIMESTAMP}.md"
TEMP_DIR="/tmp/phase2_f1_$$"
mkdir -p "$TEMP_DIR"

################################################################################
# F1. Fallback-Only Performance Test
################################################################################

log_section "F1. Fallback-Only Performance Test"

{
    echo "# Phase 2 - F1 Fallback Performance Test"
    echo ""
    echo "**Test Run**: $(date -Iseconds)"
    echo "**Gateway**: $GATEWAY_URL"
    echo "**Namespace**: $NAMESPACE"
    echo ""
    echo "**v2.2 Standard**:"
    echo "- p99(degraded) < 20ms"
    echo "- degraded_ratio ≥ 0.99"
    echo "- error_rate ≤ 0.1%"
    echo ""
    echo "---"
    echo ""
    echo "## Procedure"
    echo ""
} > "$RESULT_FILE"

# Step 1: Record current inference replicas
ORIGINAL_REPLICAS=$(kubectl get deployment inference -n "$NAMESPACE" -o jsonpath='{.spec.replicas}')
log_info "Current inference replicas: $ORIGINAL_REPLICAS"

{
    echo "### Step 1: Scale Down Inference"
    echo "- Original replicas: $ORIGINAL_REPLICAS"
} >> "$RESULT_FILE"

# Step 2: Scale inference to 0
log_info "Scaling inference deployment to 0 replicas..."
kubectl scale deployment inference --replicas=0 -n "$NAMESPACE" > /dev/null

{
    echo "- Action: \`kubectl scale deployment inference --replicas=0\`"
    echo "- Timestamp: $(date -Iseconds)"
    echo ""
} >> "$RESULT_FILE"

# Step 3: Wait for pods to terminate
log_info "Waiting for inference pods to terminate..."
sleep 10

{
    echo "### Step 2: Wait for Circuit Breaker to Fully Open"
    echo "- Wait time: **5 minutes** (300s)"
    echo "- Purpose: Ensure all gateway instances detect inference failure and open circuit breaker"
    echo "- Start time: $(date -Iseconds)"
    echo ""
} >> "$RESULT_FILE"

# Print countdown every 30s
log_info "Waiting 5 minutes for circuit breaker to fully open..."
for i in {10..1}; do
    log_info "  ... $((i * 30)) seconds remaining"
    sleep 30
done

{
    echo "- End time: $(date -Iseconds)"
    echo ""
} >> "$RESULT_FILE"

log_success "Circuit breaker should now be fully open"

# Step 4: Sample 200 degraded requests
log_info "Sampling 200 degraded-mode requests..."

{
    echo "### Step 3: Sample Degraded-Mode Performance"
    echo "- Sample size: 200 requests"
    echo "- Query: \"dress\", k=10"
    echo ""
} >> "$RESULT_FILE"

SAMPLE_SIZE=200
LATENCY_FILE="$TEMP_DIR/degraded_latencies.txt"
RESPONSES_FILE="$TEMP_DIR/degraded_responses.txt"

ray_hits=0
degraded_responses=0
errors=0

for ((i=1; i<=SAMPLE_SIZE; i++)); do
    echo -n "."
    [[ $((i % 50)) -eq 0 ]] && echo " [$i/$SAMPLE_SIZE]"
    
    # Use perl for millisecond-precision timing (macOS compatible)
    start_ms=$(perl -MTime::HiRes=time -e 'printf "%.0f", time * 1000')
    response=$(http_get "$GATEWAY_URL/api/recommendation/search?query=dress&k=10" 5)
    end_ms=$(perl -MTime::HiRes=time -e 'printf "%.0f", time * 1000')
    
    latency=$((end_ms - start_ms))
    echo "$latency" >> "$LATENCY_FILE"
    
    # Parse response
    if echo "$response" | jq -e '.data | length > 0' > /dev/null 2>&1; then
        source=$(echo "$response" | jq -r '.data[0].source // "unknown"')
        degraded=$(echo "$response" | jq -r '.data[0].degraded // false')
        
        if [[ "$source" == "ray" ]]; then
            ray_hits=$((ray_hits + 1))
        elif [[ "$degraded" == "true" ]]; then
            degraded_responses=$((degraded_responses + 1))
        fi
        
        echo "$response" >> "$RESPONSES_FILE"
    else
        errors=$((errors + 1))
    fi
done

echo ""

# Calculate statistics
log_info "Calculating statistics..."

stats=$(calculate_stats "degraded_latency" "$LATENCY_FILE")
p50=$(echo "$stats" | jq -r '.p50')
p95=$(echo "$stats" | jq -r '.p95')
p99=$(echo "$stats" | jq -r '.p99')
min=$(echo "$stats" | jq -r '.min')
max=$(echo "$stats" | jq -r '.max')
avg=$(echo "$stats" | jq -r '.avg')

degraded_ratio=$(calculate_ratio $degraded_responses $SAMPLE_SIZE)
error_rate=$(calculate_ratio $errors $SAMPLE_SIZE)

# Write results
{
    echo "### Results"
    echo ""
    echo "#### Response Distribution"
    echo "- Total requests: $SAMPLE_SIZE"
    echo "- Ray hits: $ray_hits"
    echo "- Degraded responses: $degraded_responses"
    echo "- Errors: $errors"
    echo "- **degraded_ratio**: **$degraded_ratio**"
    echo "- **error_rate**: **$error_rate**"
    echo ""
    echo "#### Latency Statistics (degraded mode)"
    echo "- **p50**: **${p50}ms**"
    echo "- **p95**: **${p95}ms**"
    echo "- **p99**: **${p99}ms**"
    echo "- min: ${min}ms"
    echo "- max: ${max}ms"
    echo "- avg: ${avg}ms"
    echo ""
} >> "$RESULT_FILE"

# Determine pass/fail based on v2.2 standards
pass_degraded_ratio=$(python3 -c "print(1 if $degraded_ratio >= 0.99 else 0)")
pass_error_rate=$(python3 -c "print(1 if $error_rate <= 0.001 else 0)")
pass_p99=$(python3 -c "print(1 if float('$p99') < 20 else 0)")

if [[ $pass_degraded_ratio -eq 1 && $pass_error_rate -eq 1 && $pass_p99 -eq 1 ]]; then
    STATUS="✅ PASS"
    EXIT_CODE=0
    log_success "F1 test PASSED"
else
    STATUS="❌ FAIL"
    EXIT_CODE=1
    log_error "F1 test FAILED"
fi

{
    echo "### v2.2 Standard Validation"
    echo ""
    echo "| Metric | Value | v2.2 Target | Status |"
    echo "|---|---|---|---|"
    echo "| **degraded_ratio** | $degraded_ratio | ≥ 0.99 | $([ $pass_degraded_ratio -eq 1 ] && echo "✅" || echo "❌") |"
    echo "| **error_rate** | $error_rate | ≤ 0.001 | $([ $pass_error_rate -eq 1 ] && echo "✅" || echo "❌") |"
    echo "| **p99(degraded)** | ${p99}ms | < 20ms | $([ $pass_p99 -eq 1 ] && echo "✅" || echo "❌") |"
    echo ""
    echo "**Status**: $STATUS"
    echo ""
} >> "$RESULT_FILE"

# Step 5: Restore inference replicas
log_info "Restoring inference deployment to $ORIGINAL_REPLICAS replicas..."
kubectl scale deployment inference --replicas=$ORIGINAL_REPLICAS -n "$NAMESPACE" > /dev/null

{
    echo "---"
    echo ""
    echo "## Cleanup"
    echo ""
    echo "### Step 4: Restore Inference Deployment"
    echo "- Action: \`kubectl scale deployment inference --replicas=$ORIGINAL_REPLICAS\`"
    echo "- Timestamp: $(date -Iseconds)"
    echo ""
} >> "$RESULT_FILE"

log_info "Waiting for inference pods to be ready..."
kubectl wait --for=condition=Ready pod -l component=inference -n "$NAMESPACE" --timeout=300s > /dev/null 2>&1 || log_warning "Some pods may still be starting"

{
    echo "- Pods ready at: $(date -Iseconds)"
    echo ""
} >> "$RESULT_FILE"

log_success "Inference deployment restored"

# Cleanup temp files
rm -rf "$TEMP_DIR"

log_section "Test Complete"
log_info "Results saved to: $RESULT_FILE"

exit $EXIT_CODE
