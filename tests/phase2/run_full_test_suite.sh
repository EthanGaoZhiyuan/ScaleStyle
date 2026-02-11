#!/usr/bin/env bash
################################################################################
# Phase 2 - Full Test Suite (Non-Destructive + B3 Degradation)
# Complete performance and metrics testing
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Timestamp for this test run
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="test-results/full_test_run_${TIMESTAMP}.log"

mkdir -p test-results

echo "================================================================"
echo "Phase 2 - Complete Test Suite"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================================"
echo ""

# Track test outcomes
declare -a tests_run=()
declare -a tests_passed=()
declare -a tests_failed=()

run_test() {
    local test_name="$1"
    local test_script="$2"
    
    echo ""
    echo ">>> 正在运行: $test_name"
    echo "================================================================"
    
    tests_run+=("$test_name")
    
    if bash "$test_script"; then
        tests_passed+=("$test_name")
        echo "✅ $test_name 通过"
    else
        tests_failed+=("$test_name")
        echo "❌ $test_name 失败"
    fi
    
    echo ""
}

# Phase 1: Gating & Functional Tests
echo "================================================================"
echo "阶段 1: 门禁和功能测试"
echo "================================================================"
run_test "A. Preflight/Gating Tests" "preflight_check.sh"
run_test "B1/B2. Functional Tests" "functional_test.sh"
run_test "C. Data Integrity Tests" "data_integrity_test.sh"

# Phase 2: Performance & Concurrency Tests
echo ""
echo "================================================================"
echo "阶段 2: 性能和并发测试"
echo "================================================================"
echo ">>> 正在运行: D/E/F. Performance & Concurrency Tests"
tests_run+=("D/E/F. Performance & Concurrency")
if python3 performance_test.py; then
    tests_passed+=("D/E/F. Performance & Concurrency")
    echo "✅ D/E/F. Performance & Concurrency 通过"
else
    tests_failed+=("D/E/F. Performance & Concurrency")
    echo "❌ D/E/F. Performance & Concurrency 失败"
fi
echo ""

# Phase 3: Observability Tests
echo "================================================================"
echo "阶段 3: 可观测性测试"
echo "================================================================"
run_test "G. Observability Tests" "observability_test.sh"

# Phase 4: Degradation Test (Disruptive but controlled)
echo ""
echo "================================================================"
echo "阶段 4: 降级测试 (⚠️ 将暂时中断推理服务)"
echo "================================================================"
echo ""
run_test "B3. Graceful Degradation" "degradation_test.sh"

# Ensure inference is restored
echo "确保推理服务已恢复..."
kubectl scale deployment inference -n scalestyle --replicas=2 2>/dev/null || true
kubectl wait --for=condition=available deployment/inference -n scalestyle --timeout=120s 2>/dev/null || true

# Summary
echo ""
echo "================================================================"
echo "测试套件总结"
echo "================================================================"
echo "总测试数: ${#tests_run[@]}"
echo "通过: ${#tests_passed[@]}"
echo "失败: ${#tests_failed[@]}"
echo ""

if [ ${#tests_passed[@]} -eq ${#tests_run[@]} ]; then
    echo "✅ 所有测试通过！"
    exit 0
elif [ ${#tests_failed[@]} -eq 0 ]; then
    echo "✅ 所有执行的测试通过！"
    exit 0
else
    echo "❌ 部分测试失败"
    echo ""
    echo "失败的测试:"
    for test in "${tests_failed[@]}"; do
        echo "  - $test"
    done
    exit 1
fi
