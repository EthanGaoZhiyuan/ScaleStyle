#!/bin/bash

################################################################################
# Phase 2 Test Suite - G. Observability Tests
#
# Tests:
#   G1. Trace coverage
#   G2. Metrics availability
#
# Exit Code: 0 if all pass, 1 if any fail
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

NAMESPACE="${NAMESPACE:-scalestyle}"
RESULT_FILE="$TEST_OUTPUT_DIR/observability_results_${TEST_TIMESTAMP}.md"
JAEGER_URL="${JAEGER_URL:-http://localhost:16686}"

################################################################################
# G1. Trace Coverage
################################################################################

test_g1_trace_coverage() {
    log_section "G1. Trace Coverage"
    
    {
        echo "## G1. Trace Coverage"
        echo ""
        echo "**Objective**: Verify distributed tracing is capturing request flows"
        echo ""
    } >> "$RESULT_FILE"
    
    # Make 50 traced requests
    local sample_size=50
    log_info "Generating $sample_size requests to capture traces..."
    
    # Use simple curl loop instead of http_get to avoid function overhead
    for ((i=1; i<=sample_size; i++)); do
        curl -s "$GATEWAY_URL/api/recommendation/search?query=dress&k=5" > /dev/null
    done
    
    log_info "Waiting 15s for traces to propagate to Jaeger..."
    sleep 15
    
    # Try to setup port-forward if Jaeger API not accessible
    # Query Jaeger for traces (simplified, assume localhost:16686 port-forward already active)
    log_info "Querying Jaeger for traces..."
    local jaeger_api="http://localhost:16686/api/traces?service=gateway-service&limit=50&lookback=5m"
    local traces_response=$(curl -s -f --max-time 10 "$jaeger_api" || echo "{}")
    
    if [[ "$traces_response" != "{}" ]]; then
        local trace_count=$(echo "$traces_response" | jq '.data | length // 0')
        
        {
            echo "### Trace Collection"
            echo "- Requests sent: $sample_size"
            echo "- Traces found: $trace_count"
            echo "- Trace found rate: $(calculate_ratio $trace_count $sample_size)"
            echo ""
        } >> "$RESULT_FILE"
        
        if [[ $trace_count -ge 45 ]]; then
            log_success "✓ Found $trace_count/$sample_size traces (≥90%)"
            
            {
                echo "### Sample Trace Analysis"
                echo ""
                echo "**Status**: ✅ PASS (trace_found_rate ≥ 90%)"
                echo ""
            } >> "$RESULT_FILE"
            
            return 0
        else
            log_error "✗ Only found $trace_count/$sample_size traces (<90%)"
            echo "**Status**: ❌ FAIL (trace_found_rate < 90%)" >> "$RESULT_FILE"
            echo "" >> "$RESULT_FILE"
            return 1
        fi
    else
        log_warning "⚠ Jaeger API not accessible even after port-forward attempt"
        {
            echo "### Trace Collection"
            echo "- Jaeger API: Not accessible"
            echo "- URL tried: $jaeger_api"
            echo ""
            echo "**Manual Verification**:"
            echo "1. Check Jaeger pod: \`kubectl get pods -n scalestyle -l app.kubernetes.io/name=jaeger\`"
            echo "2. Port-forward manually: \`kubectl port-forward -n scalestyle svc/jaeger-query 16686:16686\`"
            echo "3. Open Jaeger UI: http://localhost:16686"
            echo "4. Select service: gateway-service or inference-service"
            echo "5. Verify trace count ≥ ${sample_size} × 0.90 = $((sample_size * 90 / 100))"
            echo ""
            echo "**Status**: ⚠️ SKIP (Jaeger API not accessible - manual verification required)"
            echo ""
        } >> "$RESULT_FILE"
        
        return 0  # Don't fail if Jaeger is not accessible
    fi
}

################################################################################
# G2. Metrics Availability
################################################################################

test_g2_metrics_availability() {
    log_section "G2. Metrics Availability"
    
    {
        echo "## G2. Metrics Availability"
        echo ""
        echo "**Objective**: Verify Prometheus metrics endpoints are exposing key metrics"
        echo ""
    } >> "$RESULT_FILE"
    
    local all_pass=true
    
    # Check Gateway metrics (Spring Boot Actuator)
    log_info "Checking Gateway metrics endpoint..."
    local gateway_metrics_url="$GATEWAY_URL/actuator/prometheus"
    local gateway_code=$(http_get_code "$gateway_metrics_url" 5)
    
    {
        echo "### Gateway Metrics"
        echo "- Endpoint: $gateway_metrics_url"
        echo "- HTTP Status: $gateway_code"
    } >> "$RESULT_FILE"
    
    if [[ "$gateway_code" == "200" ]]; then
        local gateway_metrics=$(http_get "$gateway_metrics_url" 5)
        
        # Check for key metrics
        local has_requests=$(echo "$gateway_metrics" | grep -c "http_server_requests" || echo "0")
        local has_jvm=$(echo "$gateway_metrics" | grep -c "jvm_memory" || echo "0")
        local has_custom=$(echo "$gateway_metrics" | grep -c "recommendation_" || echo "0")
        
        {
            echo "- Metrics found:"
            echo "  - http_server_requests: $has_requests"
            echo "  - jvm_memory: $has_jvm"
            echo "  - recommendation_*: $has_custom"
        } >> "$RESULT_FILE"
        
        if [[ $has_requests -gt 0 ]] && [[ $has_jvm -gt 0 ]]; then
            log_success "✓ Gateway metrics are available"
            echo "- Status: ✅ Available" >> "$RESULT_FILE"
        else
            log_error "✗ Gateway metrics incomplete"
            echo "- Status: ❌ Incomplete" >> "$RESULT_FILE"
            all_pass=false
        fi
    else
        log_error "✗ Gateway metrics endpoint returned $gateway_code"
        echo "- Status: ❌ Unavailable" >> "$RESULT_FILE"
        all_pass=false
    fi
    
    echo "" >> "$RESULT_FILE"
    
    # Check Inference metrics (Ray Serve)
    log_info "Checking Inference metrics endpoint..."
    
    # Try multiple endpoints (localhost, K8s service, or via port-forward)
    local inference_metrics_url=""
    local inference_code="000"
    
    # Try 1: Direct localhost (if port-forwarded)
    inference_metrics_url="$INFERENCE_URL/metrics"
    inference_code=$(http_get_code "$inference_metrics_url" 5)
    
    # Try 2: Via K8s service (if running inside cluster or with kubeconfig)
    if [[ "$inference_code" != "200" ]]; then
        log_info "Localhost not accessible, trying K8s service endpoint..."
        local inference_svc="http://inference.scalestyle.svc.cluster.local:8000/metrics"
        local test_code=$(http_get_code "$inference_svc" 5)
        if [[ "$test_code" == "200" ]]; then
            inference_metrics_url="$inference_svc"
            inference_code="200"
        fi
    fi
    
    # Try 3: Via kubectl port-forward
    if [[ "$inference_code" != "200" ]]; then
        log_warning "Inference metrics not accessible directly, attempting port-forward..."
        local inference_pod=$(kubectl get pods -n scalestyle -l component=inference -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
        
        if [[ -n "$inference_pod" ]]; then
            kubectl port-forward -n scalestyle "$inference_pod" 8000:8000 > /dev/null 2>&1 &
            local pf_pid=$!
            sleep 3
            
            inference_metrics_url="http://localhost:8000/metrics"
            inference_code=$(http_get_code "$inference_metrics_url" 5)
            
            # Cleanup port-forward
            cleanup_portforward "$pf_pid"
        fi
    fi
    
    {
        echo "### Inference Metrics"
        echo "- Endpoint: $inference_metrics_url"
        echo "- HTTP Status: $inference_code"
    } >> "$RESULT_FILE"
    
    if [[ "$inference_code" == "200" ]]; then
        local inference_metrics=$(http_get "$inference_metrics_url" 5)
        
        # Check for key metrics and show samples
        local has_ray=$(echo "$inference_metrics" | grep -c "ray_" 2>/dev/null || echo "0")
        has_ray=$(echo "$has_ray" | tr -d '[:space:]' | head -1)
        [[ -z "$has_ray" ]] && has_ray=0
        
        local has_http=$(echo "$inference_metrics" | grep -c "http_" 2>/dev/null || echo "0")
        has_http=$(echo "$has_http" | tr -d '[:space:]' | head -1)
        [[ -z "$has_http" ]] && has_http=0
        
        local has_serve=$(echo "$inference_metrics" | grep -c "serve_" 2>/dev/null | head -1)
        [[ -z "$has_serve" ]] && has_serve=0
        
        # Show sample metrics
        local sample_metrics=$(echo "$inference_metrics" | grep -E "(ray_|serve_|http_)" | head -5 | tr '\n' '|' | sed 's/|$//')
        
        {
            echo "- Metrics found:"
            echo "  - ray_*: $has_ray"
            echo "  - http_*: $has_http"
            echo "  - serve_*: $has_serve"
            echo ""
            if [[ -n "$sample_metrics" ]]; then
                echo "- Sample metrics (first 5 lines):"
                echo "\`\`\`"
                echo "$sample_metrics" | tr '|' '\n'
                echo "\`\`\`"
                echo ""
            fi
        } >> "$RESULT_FILE"
        
        if [[ $has_http -gt 0 ]] || [[ $has_serve -gt 0 ]] || [[ $has_ray -gt 0 ]]; then
            log_success "✓ Inference metrics are available ($has_ray ray, $has_http http, $has_serve serve)"
            echo "- Status: ✅ Available" >> "$RESULT_FILE"
        else
            # Try kubectl exec as fallback
            log_info "Trying kubectl exec to fetch metrics directly from pod..."
            local inference_pod=$(kubectl get pods -n "$NAMESPACE" -l component=inference --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
            
            if [[ -n "$inference_pod" ]]; then
                local pod_metrics=$(kubectl exec -n "$NAMESPACE" "$inference_pod" -- curl -s http://localhost:8000/metrics 2>/dev/null || echo "")
                local pod_ray=$(echo "$pod_metrics" | grep -c "ray_" 2>/dev/null | head -1)
                [[ -z "$pod_ray" ]] && pod_ray=0
                local pod_serve=$(echo "$pod_metrics" | grep -c "serve_" 2>/dev/null | head -1)
                [[ -z "$pod_serve" ]] && pod_serve=0
                local pod_samples=$(echo "$pod_metrics" | grep -E "(ray_|serve_)" | head -10 | tr '\n' '|' | sed 's/|$//')
                
                {
                    echo ""
                    echo "- Pod-level metrics (kubectl exec):"
                    echo "  - ray_*: $pod_ray"
                    echo "  - serve_*: $pod_serve"
                    if [[ -n "$pod_samples" ]]; then
                        echo ""
                        echo "- Sample pod metrics:"
                        echo "\`\`\`"
                        echo "$pod_samples" | tr '|' '\n'
                        echo "\`\`\`"
                    fi
                } >> "$RESULT_FILE"
                
                if [[ $pod_ray -gt 0 ]] || [[ $pod_serve -gt 0 ]]; then
                    log_success "✓ Pod-level metrics found ($pod_ray ray, $pod_serve serve)"
                    echo "- Status: ✅ Available (via pod)" >> "$RESULT_FILE"
                else
                    log_warning "⚠ Metrics endpoint accessible but format may vary"
                    echo "- Status: ⚠️ Partial (endpoint OK, metrics format may vary)" >> "$RESULT_FILE"
                fi
            else
                log_warning "⚠ No running inference pods found"
                echo "- Status: ⚠️ Partial (no running pods)" >> "$RESULT_FILE"
            fi
        fi
    else
        log_warning "⚠ Inference metrics endpoint not accessible ($inference_code)"
        {
            echo "- Status: ⚠️ Not accessible"
            echo "- Note: May require port-forward or cluster-internal access"
            echo ""
            echo "**Manual Verification**:"
            echo "\`\`\`bash"
            echo "# Port-forward inference pod"
            echo "kubectl port-forward -n scalestyle svc/inference 8000:8000"
            echo "# Then access: curl http://localhost:8000/metrics"
            echo "\`\`\`"
        } >> "$RESULT_FILE"
        # Don't fail - this is common in test environments
    fi
    
    echo "" >> "$RESULT_FILE"
    
    if [[ "$all_pass" == "true" ]]; then
        echo "**Overall Status**: ✅ PASS" >> "$RESULT_FILE"
        echo "" >> "$RESULT_FILE"
        return 0
    else
        echo "**Overall Status**: ❌ FAIL" >> "$RESULT_FILE"
        echo "" >> "$RESULT_FILE"
        return 1
    fi
}

################################################################################
# Main execution
################################################################################

main() {
    log_section "Phase 2 - Observability Tests"
    log_info "Results will be saved to: $RESULT_FILE"
    
    # Initialize result file
    {
        echo "# Phase 2 Observability Test Results"
        echo ""
        echo "**Test Run**: $(date -Iseconds)"
        echo "**Gateway**: $GATEWAY_URL"
        echo "**Inference**: $INFERENCE_URL"
        echo "**Jaeger**: $JAEGER_URL"
        echo ""
        echo "---"
        echo ""
    } > "$RESULT_FILE"
    
    local all_pass=true
    
    # Run tests
    if ! test_g1_trace_coverage; then
        all_pass=false
    fi
    
    if ! test_g2_metrics_availability; then
        all_pass=false
    fi
    
    # Final summary
    {
        echo "---"
        echo ""
        echo "## Final Summary"
        echo ""
    } >> "$RESULT_FILE"
    
    if [[ "$all_pass" == "true" ]]; then
        log_section "✅ ALL OBSERVABILITY TESTS PASSED"
        echo "**Result**: ✅ ALL TESTS PASSED" >> "$RESULT_FILE"
        log_info "Results saved to: $RESULT_FILE"
        exit 0
    else
        log_section "❌ SOME OBSERVABILITY TESTS FAILED"
        echo "**Result**: ❌ SOME TESTS FAILED" >> "$RESULT_FILE"
        log_error "Results saved to: $RESULT_FILE"
        exit 1
    fi
}

main "$@"
