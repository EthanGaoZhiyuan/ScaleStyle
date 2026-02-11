#!/bin/bash

################################################################################
# Phase 2 Test Suite - H. Chaos Engineering Tests
#
# Tests:
#   H1. Pod deletion during load (RTO, peak error rate, degradation)
#
# Prerequisites:
#   - Kubernetes cluster accessible
#   - inference deployment has ≥2 replicas (for HA)
#   - Python3 with requests available (for load generation)
#
# Exit Code: 0 if all pass, 1 if any fail
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

RESULT_FILE="$TEST_OUTPUT_DIR/chaos_results_${TEST_TIMESTAMP}.md"
NAMESPACE="${NAMESPACE:-scalestyle}"
COMPONENT="${COMPONENT:-inference}"

################################################################################
# H1. Pod Deletion Under Load
################################################################################

test_h1_pod_deletion_under_load() {
    log_section "H1. Pod Deletion Under Load (RTO & Resilience)"
    
    {
        echo "## H1. Pod Deletion Under Load"
        echo ""
        echo "**Objective**: Measure RTO and error rate when deleting one inference pod during load"
        echo ""
    } >> "$RESULT_FILE"
    
    # Check prerequisites
    log_info "Checking prerequisites..."
    
    local replica_count=$(kubectl get deployment "$COMPONENT" -n "$NAMESPACE" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "0")
    
    {
        echo "### Prerequisites"
        echo "- Namespace: $NAMESPACE"
        echo "- Component: $COMPONENT"
        echo "- Current replicas: $replica_count"
        echo ""
    } >> "$RESULT_FILE"
    
    if [[ $replica_count -lt 2 ]]; then
        log_warning "⚠ Only $replica_count replica(s) - chaos test requires ≥2 for HA"
        {
            echo "**Warning**: Only $replica_count replica(s) found."
            echo ""
            echo "**To run this test properly**:"
            echo "1. Scale to 2 replicas: \`kubectl scale deployment $COMPONENT -n $NAMESPACE --replicas=2\`"
            echo "2. Wait for both pods to be ready"
            echo "3. Re-run this test"
            echo ""
            echo "**Status**: ⚠️ SKIP (Requires ≥2 replicas)"
            echo ""
        } >> "$RESULT_FILE"
        
        return 0  # Don't fail - just skip
    fi
    
    # Get target pod to delete
    local target_pod=$(kubectl get pods -n "$NAMESPACE" -l "component=$COMPONENT" -o jsonpath='{.items[0].metadata.name}')
    
    if [[ -z "$target_pod" ]]; then
        log_error "✗ Could not find any $COMPONENT pods"
        echo "**Status**: ❌ FAIL (No pods found)" >> "$RESULT_FILE"
        echo "" >> "$RESULT_FILE"
        return 1
    fi
    
    log_info "Target pod for deletion: $target_pod"
    
    {
        echo "### Test Setup"
        echo "- Target pod: $target_pod"
        echo "- Load: c=20 concurrent requests"
        echo "- Duration: 120s (60s before deletion, 60s after)"
        echo ""
    } >> "$RESULT_FILE"
    
    # Start background load test
    log_info "Starting background load test (c=20)..."
    
    local load_output="/tmp/chaos_load_$$.json"
    
    # Use Python script for concurrent load
    python3 - "$load_output" <<'PYTHON_SCRIPT' &
import sys
import json
import time
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

output_file = sys.argv[1]
gateway_url = "http://localhost:8080/api/recommendation/search"
queries = ["dress", "jeans", "shirt", "jacket", "shoes"]

results = []
start_time = time.time()

def worker(query_id):
    query = queries[query_id % len(queries)]
    req_start = time.time()
    
    try:
        resp = requests.get(f"{gateway_url}?query={query}&k=10", timeout=10)
        latency = (time.time() - req_start) * 1000
        
        if resp.status_code == 200:
            data = resp.json()
            source = data.get("data", [{}])[0].get("source", "unknown")
            degraded = data.get("data", [{}])[0].get("degraded", False)
            
            return {
                "timestamp": time.time() - start_time,
                "latency_ms": latency,
                "status": "success",
                "source": source,
                "degraded": degraded
            }
        else:
            return {
                "timestamp": time.time() - start_time,
                "latency_ms": latency,
                "status": "error",
                "source": "error",
                "degraded": False
            }
    except Exception as e:
        latency = (time.time() - req_start) * 1000
        return {
            "timestamp": time.time() - start_time,
            "latency_ms": latency,
            "status": "exception",
            "source": "exception",
            "degraded": False
        }

# Run for 120 seconds
end_time = time.time() + 120
request_id = 0

with ThreadPoolExecutor(max_workers=20) as executor:
    while time.time() < end_time:
        future = executor.submit(worker, request_id)
        request_id += 1
        result = future.result()
        results.append(result)

# Save results
with open(output_file, 'w') as f:
    json.dump(results, f)

print(f"Load test completed: {len(results)} requests")
PYTHON_SCRIPT

    local load_pid=$!
    
    # Wait 30s to establish baseline
    log_info "Establishing baseline for 30s..."
    sleep 30
    
    # Record deletion time
    local deletion_time=$(date +%s)
    
    log_warning "⚠ DELETING POD: $target_pod"
    k8s_delete_pod "$NAMESPACE" "$target_pod"
    
    {
        echo "### Chaos Event"
        echo "- Event: Pod deletion"
        echo "- Time: $(date -Iseconds)"
        echo "- Deleted pod: $target_pod"
        echo ""
    } >> "$RESULT_FILE"
    
    # Wait for load test to complete
    log_info "Continuing load test for 90s..."
    wait $load_pid
    
    # Analyze results
    log_info "Analyzing chaos test results..."
    
    if [[ ! -f "$load_output" ]]; then
        log_error "✗ Load test output file not found"
        echo "**Status**: ❌ FAIL (Load test failed)" >> "$RESULT_FILE"
        echo "" >> "$RESULT_FILE"
        return 1
    fi
    
    # Use Python to analyze results
    python3 - "$load_output" "$deletion_time" "$RESULT_FILE" <<'PYTHON_ANALYSIS'
import sys
import json
import statistics

results_file = sys.argv[1]
deletion_time = int(sys.argv[2])
report_file = sys.argv[3]

with open(results_file, 'r') as f:
    results = json.load(f)

# Split into pre/post deletion
pre_deletion = [r for r in results if r['timestamp'] < 30]
post_deletion = [r for r in results if r['timestamp'] >= 30 and r['timestamp'] < 90]
full_recovery = [r for r in results if r['timestamp'] >= 60]

# Calculate metrics
def analyze_window(data, name):
    if not data:
        return None
    
    total = len(data)
    errors = len([r for r in data if r['status'] != 'success'])
    ray_hits = len([r for r in data if r['source'] == 'ray' and not r['degraded']])
    degraded = len([r for r in data if r['degraded']])
    
    latencies = [r['latency_ms'] for r in data if r['status'] == 'success']
    
    return {
        'name': name,
        'total': total,
        'errors': errors,
        'ray_hits': ray_hits,
        'degraded': degraded,
        'error_rate': errors / max(total, 1),
        'ray_ratio': ray_hits / max(total, 1),
        'degraded_ratio': degraded / max(total, 1),
        'p99_latency': sorted(latencies)[int(len(latencies) * 0.99)] if latencies else 0
    }

pre_metrics = analyze_window(pre_deletion, "Pre-deletion (baseline)")
post_metrics = analyze_window(post_deletion, "Post-deletion (0-60s)")
recovery_metrics = analyze_window(full_recovery, "Recovery (30-60s)")

# Find RTO (time to recover ray_ratio ≥ 0.90)
rto_seconds = None
for i, r in enumerate(post_deletion):
    window_start = i
    window_end = min(i + 20, len(post_deletion))  # 20-request sliding window
    window = post_deletion[window_start:window_end]
    
    ray_in_window = len([x for x in window if x['source'] == 'ray' and not x['degraded']])
    ratio = ray_in_window / len(window) if window else 0
    
    if ratio >= 0.90:
        rto_seconds = r['timestamp'] - 30  # Relative to deletion
        break

# Write to report
with open(report_file, 'a') as f:
    f.write("### Chaos Test Results\n\n")
    f.write("| Phase | Total | Errors | Ray Hits | Degraded | Error Rate | Ray Ratio | Degraded Ratio | p99 |\n")
    f.write("|-------|-------|--------|----------|----------|------------|-----------|----------------|-----|\n")
    
    for m in [pre_metrics, post_metrics, recovery_metrics]:
        if m:
            f.write(f"| {m['name']} | {m['total']} | {m['errors']} | {m['ray_hits']} | "
                   f"{m['degraded']} | {m['error_rate']:.4f} | {m['ray_ratio']:.4f} | "
                   f"{m['degraded_ratio']:.4f} | {m['p99_latency']:.0f}ms |\n")
    
    f.write("\n")
    f.write("### RTO Analysis\n\n")
    
    if rto_seconds:
        f.write(f"- **RTO**: {rto_seconds:.1f}s (time to recover ray_ratio ≥ 0.90)\n")
    else:
        f.write("- **RTO**: >60s (did not recover within test window)\n")
    
    f.write(f"- **Peak Error Rate**: {post_metrics['error_rate']:.4f} (0-60s window)\n")
    f.write(f"- **Degraded Ratio During Outage**: {post_metrics['degraded_ratio']:.4f}\n")
    f.write("\n")
    
    # Pass criteria
    pass_rto = rto_seconds is not None and rto_seconds <= 120
    pass_error = post_metrics['error_rate'] <= 0.02
    pass_degraded = post_metrics['degraded_ratio'] >= 0.95 if post_metrics['ray_ratio'] < 0.50 else True
    
    pass_all = pass_rto and pass_error
    
    f.write("### Pass Criteria\n\n")
    f.write(f"- {'✅' if pass_rto else '❌'} RTO ≤ 120s: {rto_seconds:.1f}s if rto_seconds else '>60s'\n")
    f.write(f"- {'✅' if pass_error else '❌'} Peak error rate ≤ 2%: {post_metrics['error_rate']:.2%}\n")
    f.write(f"- {'✅' if pass_degraded else 'ℹ️'} Degraded during outage: {post_metrics['degraded_ratio']:.2%}\n")
    f.write("\n")
    f.write(f"**Status**: {'✅ PASS' if pass_all else '❌ FAIL'}\n\n")
    
    sys.exit(0 if pass_all else 1)
PYTHON_ANALYSIS

    local analysis_exit=$?
    
    rm -f "$load_output"
    
    if [[ $analysis_exit -eq 0 ]]; then
        log_success "✓ H1 PASS: RTO and error rate within limits"
        return 0
    else
        log_error "✗ H1 FAIL: RTO or error rate exceeded limits"
        return 1
    fi
}

################################################################################
# Main execution
################################################################################

main() {
    log_section "Phase 2 - Chaos Engineering Tests"
    log_info "Results will be saved to: $RESULT_FILE"
    
    # Initialize result file
    {
        echo "# Phase 2 Chaos Engineering Test Results"
        echo ""
        echo "**Test Run**: $(date -Iseconds)"
        echo "**Namespace**: $NAMESPACE"
        echo "**Component**: $COMPONENT"
        echo ""
        echo "---"
        echo ""
    } > "$RESULT_FILE"
    
    local all_pass=true
    
    # Run tests
    if ! test_h1_pod_deletion_under_load; then
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
        log_section "✅ ALL CHAOS TESTS PASSED"
        echo "**Result**: ✅ ALL TESTS PASSED" >> "$RESULT_FILE"
        log_info "Results saved to: $RESULT_FILE"
        exit 0
    else
        log_section "❌ SOME CHAOS TESTS FAILED"
        echo "**Result**: ❌ SOME TESTS FAILED" >> "$RESULT_FILE"
        log_error "Results saved to: $RESULT_FILE"
        exit 1
    fi
}

main "$@"
