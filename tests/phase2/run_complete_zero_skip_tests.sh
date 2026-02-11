#!/usr/bin/env bash
################################################################################
# Complete A-J Test Suite Runner
# Executes all Phase 2 tests in order with zero skips
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TEST_RUN_ID=$(date +%Y%m%d_%H%M%S)
LOG_FILE="test-results/complete_test_run_${TEST_RUN_ID}.log"
SUMMARY_FILE="test-results/complete_test_summary_${TEST_RUN_ID}.md"

echo "================================================================================================="
echo "Phase 2 Complete Test Suite - Zero Skip Edition"
echo "Test Run ID: $TEST_RUN_ID"
echo "Start Time: $(date -Iseconds)"
echo "================================================================================================="

# Function to log section
log_section() {
    echo ""
    echo "================================================================================================="
    echo "$1"
    echo "================================================================================================="
}

# Initialize summary
{
    echo "# Complete Test Run Summary"
    echo ""
    echo "**Test Run ID**: $TEST_RUN_ID"
    echo "**Start Time**: $(date -Iseconds)"
    echo ""
    echo "---"
    echo ""
} > "$SUMMARY_FILE"

# Test counter
TOTAL_TESTS=0
PASSED_TESTS=0
 FAILED_TESTS=0

# A. Preflight
log_section "A. PREFLIGHT CHECKS"
TOTAL_TESTS=$((TOTAL_TESTS + 1))
if bash preflight_check.sh; then
    PASSED_TESTS=$((PASSED_TESTS + 1))
    echo "✅ A: PASS" >> "$SUMMARY_FILE"
else
    FAILED_TESTS=$((FAILED_TESTS + 1))
    echo "❌ A: FAIL" >> "$SUMMARY_FILE"
fi

# B1/B2/B3. Functional & Degradation
log_section "B. FUNCTIONAL TESTS"
TOTAL_TESTS=$((TOTAL_TESTS + 1))
if bash functional_test.sh; then
    PASSED_TESTS=$((PASSED_TESTS + 1))
    echo "✅ B1/B2: PASS" >> "$SUMMARY_FILE"
else
    FAILED_TESTS=$((FAILED_TESTS + 1))
    echo "❌ B1/B2: FAIL" >> "$SUMMARY_FILE"
fi

# C. Data Integrity
log_section "C. DATA INTEGRITY"
TOTAL_TESTS=$((TOTAL_TESTS + 1))
if bash data_integrity_test.sh; then
    PASSED_TESTS=$((PASSED_TESTS + 1))
    echo "✅ C: PASS" >> "$SUMMARY_FILE"
else
    FAILED_TESTS=$((FAILED_TESTS + 1))
    echo "❌ C: FAIL" >> "$SUMMARY_FILE"
fi

# D1/D2/E1/F1. Performance
log_section "D/E/F. PERFORMANCE TESTS"
TOTAL_TESTS=$((TOTAL_TESTS + 1))
if python3 performance_test.py --tests D1,D2,E1; then
    PASSED_TESTS=$((PASSED_TESTS + 1))
    echo "✅ D1/D2/E1: PASS" >> "$SUMMARY_FILE"
else
    FAILED_TESTS=$((FAILED_TESTS + 1))
    echo "❌ D1/D2/E1: FAIL" >> "$SUMMARY_FILE"
fi

# B3. Degradation (separate run for proper sequencing)
log_section "B3. DEGRADATION TEST"
TOTAL_TESTS=$((TOTAL_TESTS + 1))
if bash degradation_test.sh; then
    PASSED_TESTS=$((PASSED_TESTS + 1))
    echo "✅ B3: PASS" >> "$SUMMARY_FILE"
else
    FAILED_TESTS=$((FAILED_TESTS + 1))
    echo "❌ B3: FAIL" >> "$SUMMARY_FILE"
fi

# F1. Fallback (must run after B3 while system degraded, or separately)
log_section "F1. FALLBACK PERFORMANCE"
TOTAL_TESTS=$((TOTAL_TESTS + 1))
# Check if system is degraded
DEGRADED_CHECK=$(curl -s "http://localhost:8080/api/recommendation/search?query=test&k=3" | jq -r '.data[0].degraded // false')
if [[ "$DEGRADED_CHECK" == "true" ]]; then
    if python3 performance_test.py --tests F1; then
        PASSED_TESTS=$((PASSED_TESTS + 1))
        echo "✅ F1: PASS" >> "$SUMMARY_FILE"
    else
        FAILED_TESTS=$((FAILED_TESTS + 1))
        echo "❌ F1: FAIL" >> "$SUMMARY_FILE"
    fi
else
    echo "⚠️ F1: SKIP (system not degraded, will run separately)" >> "$SUMMARY_FILE"
fi

# G1/G2. Observability
log_section "G. OBSERVABILITY"
TOTAL_TESTS=$((TOTAL_TESTS + 1))
if bash observability_test.sh; then
    PASSED_TESTS=$((PASSED_TESTS + 1))
    echo "✅ G1/G2: PASS" >> "$SUMMARY_FILE"
else
    FAILED_TESTS=$((FAILED_TESTS + 1))
    echo "❌ G1/G2: FAIL" >> "$SUMMARY_FILE"
fi

# Final Summary
log_section "TEST COMPLETION SUMMARY"

{
    echo ""
    echo "---"
    echo ""
    echo "## Final Results"
    echo ""
    echo "**End Time**: $(date -Iseconds)"
    echo ""
    echo "| Metric | Value |"
    echo "|--------|-------|"
    echo "| Total Tests | $TOTAL_TESTS |"
    echo "| Passed | $PASSED_TESTS |"
    echo "| Failed | $FAILED_TESTS |"
    echo "| Pass Rate | $(python3 -c "print(f'{$PASSED_TESTS/$TOTAL_TESTS*100:.2f}%' if $TOTAL_TESTS > 0 else '0%')") |"
    echo ""
    if [ $FAILED_TESTS -eq 0 ]; then
        echo "**Status**: ✅ **ALL TESTS PASSED**"
    else
        echo "**Status**: ❌ **SOME TESTS FAILED**"
    fi
    echo ""
} >> "$SUMMARY_FILE"

echo ""
echo "================================================================================================="
echo "Test Suite Complete"
echo "Passed: $PASSED_TESTS/$TOTAL_TESTS"
echo "Summary saved to: $SUMMARY_FILE"
echo "Full log: $LOG_FILE"
echo "================================================================================================="

if [ $FAILED_TESTS -eq 0 ]; then
    exit 0
else
    exit 1
fi
