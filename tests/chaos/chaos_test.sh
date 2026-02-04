#!/bin/bash
# Chaos Testing Script for ScaleStyle
# Usage: ./infrastructure/k8s/chaos/chaos_test.sh [experiment_name]
#
# Available experiments:
#   gateway   - Delete gateway pod (test HPA + service resilience)
#   inference - Delete inference pod (test circuit breaker + degradation)
#   redis     - Delete redis pod (test cache failure handling)
#   milvus    - Delete milvus pod (test vector search fallback)
#   all       - Run all experiments sequentially

set -e

NAMESPACE="scalestyle"
EXPERIMENT=${1:-"help"}

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

function log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

function log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

function log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

function print_header() {
    echo ""
    echo "=========================================="
    echo "  $1"
    echo "=========================================="
    echo ""
}

function wait_for_pod_ready() {
    local label=$1
    local timeout=${2:-60}
    
    log_info "Waiting for pod with label '$label' to be Ready (timeout: ${timeout}s)..."
    kubectl wait --for=condition=Ready pod -l "$label" -n $NAMESPACE --timeout=${timeout}s
    if [ $? -eq 0 ]; then
        log_info "Pod is Ready!"
        return 0
    else
        log_error "Pod not Ready within timeout"
        return 1
    fi
}

function get_pod_age() {
    local label=$1
    kubectl get pods -n $NAMESPACE -l "$label" -o jsonpath='{.items[0].metadata.creationTimestamp}'
}

function experiment_gateway() {
    print_header "EXPERIMENT 1: Delete Gateway Pod"
    
    log_info "Current gateway pods:"
    kubectl get pods -n $NAMESPACE -l component=gateway
    
    log_warn "Deleting gateway pod in 5 seconds..."
    sleep 5
    
    before_age=$(get_pod_age "component=gateway")
    log_info "Before delete - Pod created at: $before_age"
    
    kubectl delete pod -n $NAMESPACE -l component=gateway --grace-period=0 --force
    
    log_info "Pod deleted. Monitoring recovery..."
    start_time=$(date +%s)
    
    wait_for_pod_ready "component=gateway" 60
    
    end_time=$(date +%s)
    rto=$((end_time - start_time))
    
    after_age=$(get_pod_age "component=gateway")
    log_info "After recovery - Pod created at: $after_age"
    log_info "RTO (Recovery Time Objective): ${rto}s"
    
    if [ $rto -le 30 ]; then
        log_info "✅ PASS: RTO <= 30s"
    else
        log_warn "⚠️  WARNING: RTO > 30s"
    fi
    
    log_info "Testing endpoint..."
    sleep 5
    curl -s http://localhost:8080/api/recommendation/search?query=test&k=5 | jq '.code'
}

function experiment_inference() {
    print_header "EXPERIMENT 2: Delete Inference Pod (Critical - Tests Circuit Breaker)"
    
    log_info "Current inference pods:"
    kubectl get pods -n $NAMESPACE -l component=inference
    
    log_warn "This will trigger circuit breaker and degradation mode"
    log_warn "Deleting inference pod in 5 seconds..."
    sleep 5
    
    before_age=$(get_pod_age "component=inference")
    log_info "Before delete - Pod created at: $before_age"
    
    kubectl delete pod -n $NAMESPACE -l component=inference --grace-period=0 --force
    
    log_info "Pod deleted. Watch for degraded=true in responses..."
    log_info "Gateway should fallback to Redis/Popular items"
    start_time=$(date +%s)
    
    # Test degradation immediately
    sleep 2
    log_info "Testing degradation (should see degraded=true or source=redis):"
    curl -s http://localhost:8080/api/recommendation/search?query=test&k=3 | jq '.data[0] | {source, degraded, degradedReason}'
    
    wait_for_pod_ready "component=inference" 120
    
    end_time=$(date +%s)
    rto=$((end_time - start_time))
    
    after_age=$(get_pod_age "component=inference")
    log_info "After recovery - Pod created at: $after_age"
    log_info "RTO: ${rto}s"
    
    log_info "Waiting for circuit breaker to close (30s)..."
    sleep 30
    
    log_info "Testing recovery (should see degraded=false, source=ray):"
    curl -s http://localhost:8080/api/recommendation/search?query=test&k=3 | jq '.data[0] | {source, degraded}'
    
    if [ $rto -le 60 ]; then
        log_info "✅ PASS: Inference recovered"
    else
        log_warn "⚠️  WARNING: Long recovery time"
    fi
}

function experiment_redis() {
    print_header "EXPERIMENT 3: Delete Redis Pod"
    
    log_info "Current redis pods:"
    kubectl get pods -n $NAMESPACE -l component=redis
    
    log_warn "Redis stores cache + popular items + metadata"
    log_warn "Deleting redis pod in 5 seconds..."
    sleep 5
    
    kubectl delete pod -n $NAMESPACE -l component=redis --grace-period=0 --force
    
    log_info "Pod deleted. Monitoring recovery..."
    start_time=$(date +%s)
    
    wait_for_pod_ready "component=redis" 60
    
    end_time=$(date +%s)
    rto=$((end_time - start_time))
    
    log_info "RTO: ${rto}s"
    log_warn "Note: Redis data is lost (no PVC). Need to re-run init-job if needed."
    
    log_info "Current pods status:"
    kubectl get pods -n $NAMESPACE
}

function experiment_milvus() {
    print_header "EXPERIMENT 4: Delete Milvus Pod"
    
    log_info "Current milvus pods:"
    kubectl get pods -n $NAMESPACE -l component=milvus
    
    log_warn "Milvus stores vector embeddings for semantic search"
    log_warn "Deleting milvus pod in 5 seconds..."
    sleep 5
    
    kubectl delete pod -n $NAMESPACE -l component=milvus --grace-period=0 --force
    
    log_info "Pod deleted. System should fallback to popularity-based recommendations"
    start_time=$(date +%s)
    
    log_info "Testing (may see slower response or fallback):"
    curl -s http://localhost:8080/api/recommendation/search?query=test&k=3 | jq '.data[0] | {source, degraded}'
    
    wait_for_pod_ready "component=milvus" 120
    
    end_time=$(date +%s)
    rto=$((end_time - start_time))
    
    log_info "RTO: ${rto}s"
    log_warn "Note: Milvus data is lost (no PVC). Vector search may not work until re-init."
}

function show_help() {
    echo "Usage: $0 [experiment]"
    echo ""
    echo "Available experiments:"
    echo "  gateway   - Delete gateway pod (test HPA + service resilience)"
    echo "  inference - Delete inference pod (test circuit breaker + degradation)"
    echo "  redis     - Delete redis pod (test cache failure handling)"
    echo "  milvus    - Delete milvus pod (test vector search fallback)"
    echo "  all       - Run all experiments sequentially (with 2min pause between)"
    echo ""
    echo "Prerequisites:"
    echo "  1. kubectl configured with scalestyle namespace"
    echo "  2. Port-forward running: kubectl port-forward -n scalestyle svc/gateway 8080:8080"
    echo "  3. Load test running (optional but recommended)"
    echo ""
    echo "Example:"
    echo "  $0 inference"
}

# Main execution
case $EXPERIMENT in
    gateway)
        experiment_gateway
        ;;
    inference)
        experiment_inference
        ;;
    redis)
        experiment_redis
        ;;
    milvus)
        experiment_milvus
        ;;
    all)
        experiment_gateway
        sleep 120
        experiment_inference
        sleep 120
        experiment_redis
        sleep 120
        experiment_milvus
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        log_error "Unknown experiment: $EXPERIMENT"
        show_help
        exit 1
        ;;
esac
