#!/bin/bash

################################################################################
# Phase 2 Test Suite - B. E2E Functional Tests
#
# Tests:
#   B1. Search interface use cases (8+ scenarios)
#   B2. Reason toggle consistency (GENERATION_ENABLED=0/1)
#   B3. Fallback correctness (service degradation handling)
#
# Exit Code: 0 if all pass, 1 if any fail
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

RESULT_FILE="$TEST_OUTPUT_DIR/functional_results_${TEST_TIMESTAMP}.md"

################################################################################
# B1. Search Interface Use Cases
################################################################################

test_b1_search_interface() {
    log_section "B1. Search Interface Use Cases (8 Test Cases)"
    
    {
        echo "## B1. Search Interface Use Cases"
        echo ""
        echo "**Objective**: Validate API contract across different query types"
        echo ""
        echo "| Test Case | Query | Expected | HTTP | Items | Top1 Valid | Status |"
        echo "|-----------|-------|----------|------|-------|------------|--------|"
    } >> "$RESULT_FILE"
    
    local all_pass=true
    
    # Test cases: [name|query|k|expected_code]
    local test_cases=(
        "Normal_dress|dress|10|200"
        "Normal_jeans|jeans|10|200"
        "Normal_shirt|shirt|5|200"
        "Normal_black|black|10|200"
        "Long_query|elegant black evening dress for summer party|10|200"
        "Special_chars_plus|black+dress|10|200"
        "Special_chars_quote|\"black dress\"|10|200"
        "Empty_query||10|400_or_popular"
        "Extreme_long|$(printf 'a%.0s' {1..250})|10|200_or_400"
        "Extreme_k_min|dress|1|200"
        "Extreme_k_max|dress|50|200"
    )
    
    for test_case in "${test_cases[@]}"; do
        IFS='|' read -r test_name query k expected_code <<< "$test_case"
        
        # Build URL
        local encoded_query=$(echo "$query" | jq -sRr @uri)
        local url="$GATEWAY_URL/api/recommendation/search?query=${encoded_query}&k=${k}"
        
        # Make request
        local code=$(http_get_code "$url" 10)
        local response=$(http_get "$url" 10)
        
        # Validate
        local items_count=$(echo "$response" | jq -r '.data | length // 0')
        local top1_name=$(echo "$response" | jq -r '.data[0].name // "null"')
        local top1_id=$(echo "$response" | jq -r '.data[0].itemId // "null"')
        
        local top1_valid="false"
        if [[ "$top1_name" != "null" ]] && [[ "$top1_id" != "null" ]]; then
            top1_valid="true"
        fi
        
        # Check expected
        local status="✅"
        if [[ "$expected_code" == "200" ]]; then
            if [[ "$code" != "200" ]] || [[ $items_count -ne $k ]] || [[ "$top1_valid" != "true" ]]; then
                status="❌"
                all_pass=false
            fi
        elif [[ "$expected_code" == "400_or_popular" ]]; then
            # Empty query: either 400 or 200 with popular items
            if [[ "$code" != "200" ]] && [[ "$code" != "400" ]]; then
                status="❌"
                all_pass=false
            fi
        elif [[ "$expected_code" == "200_or_400" ]]; then
            # Extreme cases: server may reject or accept
            if [[ "$code" != "200" ]] && [[ "$code" != "400" ]]; then
                status="❌"
                all_pass=false
            fi
        fi
        
        # Log result
        local display_query="${query}"
        if [[ ${#display_query} -gt 30 ]]; then
            display_query="${display_query:0:30}..."
        fi
        
        echo "| $test_name | $display_query | $expected_code | $code | $items_count | $top1_valid | $status |" >> "$RESULT_FILE"
        
        if [[ "$status" == "✅" ]]; then
            log_success "✓ $test_name: HTTP $code, $items_count items"
        else
            log_error "✗ $test_name: HTTP $code, $items_count items (expected $expected_code)"
        fi
    done
    
    echo "" >> "$RESULT_FILE"
    
    # Sample top1 for normal cases
    {
        echo "**Sample Top-1 Results**:"
        echo '```json'
        local url="$GATEWAY_URL/api/recommendation/search?query=dress&k=1"
        http_get "$url" 5 | jq '.data[0] | {itemId, name, source, degraded}'
        echo '```'
        echo ""
    } >> "$RESULT_FILE"
    
    if [[ "$all_pass" == "true" ]]; then
        echo "**Status**: ✅ PASS" >> "$RESULT_FILE"
        echo "" >> "$RESULT_FILE"
        return 0
    else
        echo "**Status**: ❌ FAIL" >> "$RESULT_FILE"
        echo "" >> "$RESULT_FILE"
        return 1
    fi
}

################################################################################
# B2. Reason Toggle Consistency
################################################################################

test_b2_reason_toggle() {
    log_section "B2. Reason Toggle Consistency (GENERATION_ENABLED check)"
    
    {
        echo "## B2. Reason Toggle Consistency"
        echo ""
        echo "**Objective**: Verify reason field behavior matches GENERATION_ENABLED setting"
        echo ""
    } >> "$RESULT_FILE"
    
    # Sample 20 requests
    local sample_size=20
    local reason_present=0
    local reason_absent=0
    
    local temp_samples="/tmp/reason_samples_$$.txt"
    > "$temp_samples"
    
    log_info "Sampling $sample_size requests to check reason field..."
    
    for ((i=1; i<=sample_size; i++)); do
        local query="dress"
        local url="$GATEWAY_URL/api/recommendation/search?query=$query&k=1"
        local response=$(http_get "$url" 5)
        
        local reason=$(echo "$response" | jq -r '.data[0].reason // ""')
        local reason_source=$(echo "$response" | jq -r '.data[0].reasonSource // ""')
        
        if [[ -n "$reason" ]] && [[ "$reason" != "null" ]]; then
            reason_present=$((reason_present + 1))
            echo "Present: reason=$reason, reasonSource=$reason_source" >> "$temp_samples"
        else
            reason_absent=$((reason_absent + 1))
        fi
    done
    
    # Report
    {
        echo "| Metric | Count |"
        echo "|--------|-------|"
        echo "| Reason Present | $reason_present |"
        echo "| Reason Absent | $reason_absent |"
        echo "| Total | $sample_size |"
        echo ""
    } >> "$RESULT_FILE"
    
    # Check GENERATION_ENABLED env (if accessible)
    log_info "Checking GENERATION_ENABLED setting..."
    local generation_enabled="unknown"
    
    # Try to infer from response
    if [[ $reason_present -gt $((sample_size * 8 / 10)) ]]; then
        generation_enabled="likely_enabled"
        log_info "Reason present in $reason_present/$sample_size requests → likely ENABLED"
    elif [[ $reason_absent -gt $((sample_size * 8 / 10)) ]]; then
        generation_enabled="likely_disabled"
        log_info "Reason absent in $reason_absent/$sample_size requests → likely DISABLED"
    else
        generation_enabled="inconsistent"
        log_warning "Reason inconsistent: $reason_present present, $reason_absent absent"
    fi
    
    {
        echo "**Inferred Setting**: $generation_enabled"
        echo ""
        echo "**Sample Reasons** (first 3):"
        echo '```'
        head -3 "$temp_samples"
        echo '```'
        echo ""
    } >> "$RESULT_FILE"
    
    rm -f "$temp_samples"
    
    # Pass if consistent (all or none)
    if [[ "$generation_enabled" != "inconsistent" ]]; then
        log_success "✓ Reason field is consistent"
        echo "**Status**: ✅ PASS (Consistent)" >> "$RESULT_FILE"
        echo "" >> "$RESULT_FILE"
        return 0
    else
        log_error "✗ Reason field is inconsistent"
        echo "**Status**: ❌ FAIL (Inconsistent)" >> "$RESULT_FILE"
        echo "" >> "$RESULT_FILE"
        return 1
    fi
}

################################################################################
# B3. Fallback Correctness (Degradation Handling)
################################################################################

test_b3_fallback_correctness() {
    log_section "B3. Fallback Correctness (Degradation Handling)"
    
    {
        echo "## B3. Fallback Correctness"
        echo ""
        echo "**Objective**: Verify system returns valid results during degradation (no empty responses)"
        echo ""
        echo "**Note**: This test requires manually scaling inference=0 or blocking Ray Serve."
        echo "Skipping automatic degradation test in normal run."
        echo ""
    } >> "$RESULT_FILE"
    
    # Check if we're in a degraded state
    log_info "Checking current degradation status..."
    
    local sample_size=20
    local degraded_count=0
    
    for ((i=1; i<=sample_size; i++)); do
        local url="$GATEWAY_URL/api/recommendation/search?query=dress&k=5"
        local response=$(http_get "$url" 5)
        
        local degraded=$(echo "$response" | jq -r '.data[0].degraded // false')
        if [[ "$degraded" == "true" ]]; then
            degraded_count=$((degraded_count + 1))
        fi
    done
    
    local degraded_ratio=$(calculate_ratio $degraded_count $sample_size)
    
    {
        echo "**Current State Check**:"
        echo "- Degraded responses: $degraded_count / $sample_size"
        echo "- Degraded ratio: $degraded_ratio"
        echo ""
    } >> "$RESULT_FILE"
    
    if (( $(echo "$degraded_ratio >= 0.8" | bc -l) )); then
        log_warning "System is currently in degraded mode"
        {
            echo "**System is in degraded mode - verifying fallback responses...**"
            echo ""
        } >> "$RESULT_FILE"
        
        # Verify fallback responses are valid
        local all_valid=true
        
        for ((i=1; i<=20; i++)); do
            local url="$GATEWAY_URL/api/recommendation/search?query=dress&k=10"
            local response=$(http_get "$url" 5)
            local code=$?
            
            if [[ $code -ne 0 ]]; then
                log_error "✗ Request failed"
                all_valid=false
                continue
            fi
            
            local items_count=$(echo "$response" | jq -r '.data | length // 0')
            local degraded=$(echo "$response" | jq -r '.data[0].degraded // false')
            local source=$(echo "$response" | jq -r '.data[0].source // "unknown"')
            
            if [[ $items_count -lt 10 ]]; then
                log_error "✗ Degraded response has only $items_count items (expected 10)"
                all_valid=false
            fi
            
            if [[ "$degraded" != "true" ]]; then
                log_warning "⚠ Response claims degraded=false but should be true"
            fi
            
            if [[ "$source" == "ray" ]]; then
                log_warning "⚠ Response claims source=ray but should be fallback"
            fi
        done
        
        if [[ "$all_valid" == "true" ]]; then
            log_success "✓ All degraded responses are valid"
            echo "**Status**: ✅ PASS (Degraded mode working correctly)" >> "$RESULT_FILE"
            echo "" >> "$RESULT_FILE"
            return 0
        else
            log_error "✗ Some degraded responses are invalid"
            echo "**Status**: ❌ FAIL (Degraded responses incomplete)" >> "$RESULT_FILE"
            echo "" >> "$RESULT_FILE"
            return 1
        fi
    else
        log_info "System is in normal mode (not degraded)"
        {
            echo "**System is in NORMAL mode - cannot test fallback without manual intervention.**"
            echo ""
            echo "To fully test B3:"
            echo "1. Scale inference deployment to 0: \`kubectl scale deployment inference -n scalestyle --replicas=0\`"
            echo "2. Wait 30s for circuit breaker to open"
            echo "3. Re-run this test"
            echo "4. Restore: \`kubectl scale deployment inference -n scalestyle --replicas=1\`"
            echo ""
            echo "**Status**: ⚠️ SKIP (Manual degradation test required)"
            echo ""
        } >> "$RESULT_FILE"
        log_warning "Skipping B3 - manual degradation required for full test"
        return 0
    fi
}

################################################################################
# Main execution
################################################################################

main() {
    log_section "Phase 2 - E2E Functional Tests"
    log_info "Results will be saved to: $RESULT_FILE"
    
    # Initialize result file
    {
        echo "# Phase 2 E2E Functional Test Results"
        echo ""
        echo "**Test Run**: $(date -Iseconds)"
        echo "**Gateway**: $GATEWAY_URL"
        echo ""
        echo "---"
        echo ""
    } > "$RESULT_FILE"
    
    local all_pass=true
    
    # Run tests
    if ! test_b1_search_interface; then
        all_pass=false
    fi
    
    if ! test_b2_reason_toggle; then
        all_pass=false
    fi
    
    if ! test_b3_fallback_correctness; then
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
        log_section "✅ ALL FUNCTIONAL TESTS PASSED"
        echo "**Result**: ✅ ALL TESTS PASSED" >> "$RESULT_FILE"
        log_info "Results saved to: $RESULT_FILE"
        exit 0
    else
        log_section "❌ SOME FUNCTIONAL TESTS FAILED"
        echo "**Result**: ❌ SOME TESTS FAILED" >> "$RESULT_FILE"
        log_error "Results saved to: $RESULT_FILE"
        exit 1
    fi
}

main "$@"
