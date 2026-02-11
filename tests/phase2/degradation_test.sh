#!/usr/bin/env bash
################################################################################
# B3. Degradation/Fallback Testing
# Tests system behavior when Milvus/Ray is unavailable
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
RESULT_FILE="$TEST_OUTPUT_DIR/degradation_results_$(date +%Y%m%d_%H%M%S).md"
NAMESPACE="${NAMESPACE:-scalestyle}"

################################################################################
# Main Test Function
################################################################################

main() {
    log_section "Phase 2 - Degradation/Fallback Tests (B3)"
    
    {
        echo "# Phase 2 Degradation/Fallback Test Results"
        echo ""
        echo "**Test Run**: $(date -Iseconds)"
        echo "**Gateway**: $GATEWAY_URL"
        echo "**Namespace**: $NAMESPACE"
        echo ""
        echo "---"
        echo ""
    } > "$RESULT_FILE"
    
    log_info "Results will be saved to: $RESULT_FILE"
    
    # B3.1: Test inference down scenario
    test_b3_1_inference_down
    
    echo ""
    log_section "Test Summary"
    
    {
        echo "---"
        echo ""
        echo "## Final Summary"
        echo ""
        echo "**Result**: See individual test results above"
        echo ""
    } >> "$RESULT_FILE"
    
    log_info "Results saved to: $RESULT_FILE"
}

################################################################################
# B3.1: Inference Down (Scale to 0)
################################################################################

test_b3_1_inference_down() {
    log_section "B3.1. Inference Down - Degraded Mode Test"
    
    {
        echo "## B3.1. Inference Down - Degraded Mode Test"
        echo ""
        echo "**Objective**: Verify system gracefully degrades when inference pods are unavailable"
        echo ""
    } >> "$RESULT_FILE"
    
    # Get current replica count
    local original_replicas=$(kubectl get deployment inference -n "$NAMESPACE" -o jsonpath='{.spec.replicas}')
    log_info "Current inference replicas: $original_replicas"
    
    # Sample baseline (normal mode)
    log_info "Sampling baseline (normal mode) - 20 requests..."
    local baseline_degraded=0
    local baseline_ray=0
    for i in $(seq 1 20); do
        local url="$GATEWAY_URL/api/recommendation/search?query=dress&k=10"
        local response=$(http_get "$url" 5)
        local degraded=$(echo "$response" | jq -r '.data[0].degraded // false')
        local source=$(echo "$response" | jq -r '.data[0].source // "unknown"')
        
        if [[ "$degraded" == "true" ]]; then
            ((baseline_degraded++)) || true
        fi
        if [[ "$source" == "ray" ]]; then
            ((baseline_ray++)) || true
        fi
    done
    
    {
        echo "### Baseline (Normal Mode)"
        echo ""
        echo "| Metric | Value |"
        echo "|--------|-------|"
        echo "| Total requests | 20 |"
        echo "| Ray hits | $baseline_ray |"
        echo "| Degraded | $baseline_degraded |"
        echo "| Degraded ratio | $(python3 -c "print(f'{$baseline_degraded/20:.4f}')") |"
        echo ""
    } >> "$RESULT_FILE"
    
    # Scale inference to 0
    log_warning "⚠ Scaling inference to 0 replicas..."
    kubectl scale deployment inference -n "$NAMESPACE" --replicas=0
    
    # Wait for pods to terminate and verify
    log_info "Waiting for pods to terminate..."
    kubectl wait --for=delete pod -l component=inference -n "$NAMESPACE" --timeout=90s 2>/dev/null || true
    log_info "Waiting additional 60s for circuit breaker to trigger..."
    sleep 60
    
    # Sample degraded mode
    log_info "Sampling degraded mode - 50 requests..."
    local degraded_count=0
    local ray_count=0
    local error_count=0
    local total_items=0
    local latencies=()
    
    for i in $(seq 1 50); do
        local url="$GATEWAY_URL/api/recommendation/search?query=dress&k=10"
        local start=$(python3 -c "import time; print(int(time.time() * 1000))")
        local response=$(http_get "$url" 10)
        local end=$(python3 -c "import time; print(int(time.time() * 1000))")
        local latency=$((end - start))
        latencies+=("$latency")
        
        local http_code=$(http_get_code "$url" 10)
        if [[ "$http_code" != "200" ]]; then
            ((error_count++)) || true
            continue
        fi
        
        local degraded=$(echo "$response" | jq -r '.data[0].degraded // false')
        local source=$(echo "$response" | jq -r '.data[0].source // "unknown"')
        local items=$(echo "$response" | jq -r '.data | length // 0')
        total_items=$((total_items + items))
        
        if [[ "$degraded" == "true" ]]; then
            ((degraded_count++)) || true
        fi
        if [[ "$source" == "ray" ]]; then
            ((ray_count++)) || true
        fi
    done
    
    # Calculate statistics
    local IFS=$'\n'
    local sorted_latencies=($(printf '%s\n' "${latencies[@]}" | sort -n))
    local p50_idx=$((${#sorted_latencies[@]} * 50 / 100))
    local p99_idx=$((${#sorted_latencies[@]} * 99 / 100))
    local p50=${sorted_latencies[$p50_idx]}
    local p99=${sorted_latencies[$p99_idx]}
    local avg=$(printf '%s\n' "${latencies[@]}" | awk '{sum+=$1} END {printf "%.2f", sum/NR}')
    
    local degraded_ratio=$(python3 -c "print(f'{$degraded_count/50:.4f}')")
    local error_rate=$(python3 -c "print(f'{$error_count/50:.4f}')")
    local avg_items=$(python3 -c "print(f'{$total_items/50:.1f}')")
    
    {
        echo "### Degraded Mode (Inference Down)"
        echo ""
        echo "| Metric | Value |"
        echo "|--------|-------|"
        echo "| Total requests | 50 |"
        echo "| Ray hits | $ray_count |"
        echo "| Degraded responses | $degraded_count |"
        echo "| Errors | $error_count |"
        echo "| **Degraded ratio** | **$degraded_ratio** |"
        echo "| **Error rate** | **$error_rate** |"
        echo "| Avg items per response | $avg_items |"
        echo "| p50 latency | ${p50}ms |"
        echo "| p99 latency | ${p99}ms |"
        echo "| Avg latency | ${avg}ms |"
        echo ""
    } >> "$RESULT_FILE"
    
    # Restore inference
    log_info "Restoring inference to $original_replicas replicas..."
    kubectl scale deployment inference -n "$NAMESPACE" --replicas="$original_replicas"
    
    # Wait for deployment
    log_info "Waiting for inference to be ready..."
    kubectl wait --for=condition=available --timeout=120s deployment/inference -n "$NAMESPACE"
    
    # Validation
    local status="✅ PASS"
    if [[ $(python3 -c "print(1 if $degraded_ratio >= 0.8 else 0)") -eq 1 ]] && \
       [[ $(python3 -c "print(1 if $error_rate < 0.05 else 0)") -eq 1 ]] && \
       [[ $(python3 -c "print(1 if $avg_items >= 5 else 0)") -eq 1 ]]; then
        log_success "✓ B3.1 PASS: System degraded gracefully"
        {
            echo "### Pass Criteria"
            echo ""
            echo "- ✅ Degraded ratio ≥ 80%: ${degraded_ratio}"
            echo "- ✅ Error rate < 5%: ${error_rate}"
            echo "- ✅ Still returns items (avg ≥ 5): ${avg_items}"
            echo ""
            echo "**Status**: ✅ PASS"
            echo ""
        } >> "$RESULT_FILE"
    else
        log_error "✗ B3.1 FAIL"
        status="❌ FAIL"
        {
            echo "### Pass Criteria"
            echo ""
            echo "- Degraded ratio ≥ 80%: ${degraded_ratio}"
            echo "- Error rate < 5%: ${error_rate}"
            echo "- Still returns items (avg ≥ 5): ${avg_items}"
            echo ""
            echo "**Status**: ❌ FAIL"
            echo ""
        } >> "$RESULT_FILE"
    fi
}

################################################################################
# Entry Point
################################################################################

main "$@"
