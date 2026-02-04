#!/bin/bash
# ScaleStyle Deployment Script
# Supports both Minikube (local) and EKS (cloud) environments

set -e

ENVIRONMENT=${1:-"minikube"}
ACTION=${2:-"deploy"}

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

function log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

function log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

function log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

function show_help() {
    cat << EOF
Usage: $0 [environment] [action]

Environments:
  minikube  - Local development (default)
  eks       - AWS EKS production

Actions:
  deploy    - Deploy all resources (default)
  delete    - Delete all resources
  diff      - Show diff before deploying
  validate  - Validate kustomize manifests

Examples:
  $0 minikube deploy     # Deploy to Minikube
  $0 eks deploy          # Deploy to EKS
  $0 eks diff            # Preview EKS changes
  $0 minikube delete     # Remove from Minikube

EOF
}

function validate_environment() {
    log_info "Validating $ENVIRONMENT environment..."
    
    # Check kubectl
    if ! command -v kubectl &> /dev/null; then
        log_error "kubectl not found. Please install kubectl first."
        exit 1
    fi
    
    # Check kustomize
    if ! command -v kustomize &> /dev/null; then
        log_warn "kustomize not found. Using kubectl kustomize instead."
    fi
    
    # Check cluster connectivity
    if ! kubectl cluster-info &> /dev/null; then
        log_error "Cannot connect to Kubernetes cluster. Is it running?"
        exit 1
    fi
    
    CONTEXT=$(kubectl config current-context)
    log_info "Current context: $CONTEXT"
    
    # Validate context matches environment
    if [[ "$ENVIRONMENT" == "minikube" ]] && [[ "$CONTEXT" != "minikube" ]]; then
        log_warn "Context is '$CONTEXT' but environment is 'minikube'"
        read -p "Continue anyway? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
    
    if [[ "$ENVIRONMENT" == "eks" ]] && [[ "$CONTEXT" != *"eks"* ]]; then
        log_warn "Context is '$CONTEXT' but environment is 'eks'"
        read -p "Continue anyway? (y/N) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
}

function deploy() {
    log_info "Deploying to $ENVIRONMENT..."
    
    OVERLAY_PATH="infrastructure/k8s-kustomize/overlays/$ENVIRONMENT"
    
    if [ ! -d "$OVERLAY_PATH" ]; then
        log_error "Overlay not found: $OVERLAY_PATH"
        exit 1
    fi
    
    # Build and apply
    if command -v kustomize &> /dev/null; then
        kustomize build "$OVERLAY_PATH" | kubectl apply -f -
    else
        kubectl apply -k "$OVERLAY_PATH"
    fi
    
    log_info "Deployment complete!"
    log_info "Waiting for pods to be ready..."
    
    kubectl wait --for=condition=Ready pod --all -n scalestyle --timeout=300s || true
    
    log_info "Current status:"
    kubectl get all -n scalestyle
    
    if [[ "$ENVIRONMENT" == "minikube" ]]; then
        log_info ""
        log_info "To access services:"
        log_info "  kubectl port-forward -n scalestyle svc/local-gateway 8080:8080"
        log_info "  curl http://localhost:8080/api/recommendation/search?query=dress&k=5"
    elif [[ "$ENVIRONMENT" == "eks" ]]; then
        log_info ""
        log_info "To access services:"
        log_info "  kubectl get ingress -n scalestyle"
        log_info "  # Use the ALB DNS name from the ingress"
    fi
}

function delete_resources() {
    log_warn "Deleting resources from $ENVIRONMENT..."
    
    OVERLAY_PATH="infrastructure/k8s-kustomize/overlays/$ENVIRONMENT"
    
    if [ ! -d "$OVERLAY_PATH" ]; then
        log_error "Overlay not found: $OVERLAY_PATH"
        exit 1
    fi
    
    # Build and delete
    if command -v kustomize &> /dev/null; then
        kustomize build "$OVERLAY_PATH" | kubectl delete -f -
    else
        kubectl delete -k "$OVERLAY_PATH"
    fi
    
    log_info "Resources deleted"
}

function diff_changes() {
    log_info "Showing diff for $ENVIRONMENT..."
    
    OVERLAY_PATH="infrastructure/k8s-kustomize/overlays/$ENVIRONMENT"
    
    if command -v kustomize &> /dev/null; then
        kustomize build "$OVERLAY_PATH" | kubectl diff -f - || true
    else
        kubectl diff -k "$OVERLAY_PATH" || true
    fi
}

function validate_manifests() {
    log_info "Validating manifests for $ENVIRONMENT..."
    
    OVERLAY_PATH="infrastructure/k8s-kustomize/overlays/$ENVIRONMENT"
    
    if command -v kustomize &> /dev/null; then
        kustomize build "$OVERLAY_PATH" | kubectl apply --dry-run=client -f -
    else
        kubectl apply -k "$OVERLAY_PATH" --dry-run=client
    fi
    
    log_info "Validation passed!"
}

# Main execution
case "$1" in
    help|--help|-h)
        show_help
        exit 0
        ;;
esac

case "$ENVIRONMENT" in
    minikube|eks)
        validate_environment
        ;;
    *)
        log_error "Unknown environment: $ENVIRONMENT"
        show_help
        exit 1
        ;;
esac

case "$ACTION" in
    deploy)
        deploy
        ;;
    delete)
        delete_resources
        ;;
    diff)
        diff_changes
        ;;
    validate)
        validate_manifests
        ;;
    *)
        log_error "Unknown action: $ACTION"
        show_help
        exit 1
        ;;
esac
