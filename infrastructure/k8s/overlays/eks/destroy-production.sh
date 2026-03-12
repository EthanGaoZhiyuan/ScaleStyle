#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/../../../../" && pwd)
TF_DIR=${TF_DIR:-$ROOT_DIR/infrastructure/terraform}
NAMESPACE=${NAMESPACE:-scalestyle}
KAFKA_NAMESPACE=${KAFKA_NAMESPACE:-kafka-system}
ALB_CONTROLLER_NAMESPACE=${ALB_CONTROLLER_NAMESPACE:-kube-system}
ALB_CONTROLLER_RELEASE=${ALB_CONTROLLER_RELEASE:-aws-load-balancer-controller}
ALB_CONTROLLER_SERVICE_ACCOUNT=${ALB_CONTROLLER_SERVICE_ACCOUNT:-aws-load-balancer-controller}
MILVUS_RELEASE=${MILVUS_RELEASE:-milvus}
STRIMZI_RELEASE=${STRIMZI_RELEASE:-strimzi-kafka-operator}
EXPECTED_ENVIRONMENT="production"
INGRESS_DELETE_TIMEOUT=${INGRESS_DELETE_TIMEOUT:-600s}
KAFKA_DELETE_TIMEOUT=${KAFKA_DELETE_TIMEOUT:-900s}

TARGET_ENVIRONMENT=""
ASSUME_YES=false
SKIP_K8S_TEARDOWN=false
SKIP_TERRAFORM_DESTROY=false
TERRAFORM_ARGS=()

CLUSTER_NAME=""
AWS_REGION=""
AWS_ACCOUNT_ID=""
VPC_ID=""
CURRENT_CONTEXT=""

log_info() {
  echo "[INFO] $*"
}

log_warn() {
  echo "[WARN] $*" >&2
}

log_error() {
  echo "[ERROR] $*" >&2
}

usage() {
  cat <<EOF
Usage: $0 --environment production [options] [-- terraform destroy args]

Options:
  --environment NAME      Required. Must be "production".
  --yes                   Skip the interactive confirmation prompt.
  --skip-k8s-teardown     Skip Kubernetes and Helm teardown steps.
  --skip-terraform        Skip Terraform destroy.
  -h, --help              Show this help.

Examples:
  $0 --environment production
  $0 --environment production -- --auto-approve
  $0 --environment production --skip-k8s-teardown -- -auto-approve

This workflow is intentionally conservative:
  1. Delete the public ALB ingress first and wait for the Kubernetes object to go away.
  2. Uninstall Helm-managed components that deploy outside the main Kustomize apply path.
  3. Delete Kafka CRs before uninstalling Strimzi.
  4. Delete the EKS application overlay.
  5. Run terraform destroy.

Residual AWS resources may still require manual review, especially:
  - ALB security groups that linger while AWS finalizers finish cleanup
  - PVC/EBS volumes retained by workload-specific policies or finalizers
  - versioned S3 objects that block bucket deletion
  - ECR images that prevent repository deletion
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --environment)
        [[ $# -ge 2 ]] || { log_error "--environment requires a value"; usage; exit 1; }
        TARGET_ENVIRONMENT="$2"
        shift 2
        ;;
      --yes)
        ASSUME_YES=true
        shift
        ;;
      --skip-k8s-teardown)
        SKIP_K8S_TEARDOWN=true
        shift
        ;;
      --skip-terraform)
        SKIP_TERRAFORM_DESTROY=true
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --)
        shift
        TERRAFORM_ARGS=("$@")
        break
        ;;
      *)
        log_error "Unknown argument: $1"
        usage
        exit 1
        ;;
    esac
  done

  if [[ -z "$TARGET_ENVIRONMENT" ]]; then
    log_error "--environment is required"
    usage
    exit 1
  fi

  if [[ "$TARGET_ENVIRONMENT" != "$EXPECTED_ENVIRONMENT" ]]; then
    log_error "Unsupported environment: $TARGET_ENVIRONMENT"
    log_error "This script only targets the production EKS stack."
    exit 1
  fi
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log_error "Required command not found: $1"
    exit 1
  fi
}

validate_dependencies() {
  require_command terraform

  if [[ "$SKIP_K8S_TEARDOWN" != "true" ]]; then
    require_command kubectl
    require_command helm
    require_command aws
  fi

  if [[ "$SKIP_TERRAFORM_DESTROY" != "true" ]]; then
    require_command aws
  fi
}

tf_output_raw() {
  local output_name="$1"
  terraform -chdir="$TF_DIR" output -raw "$output_name" 2>/dev/null || true
}

load_context() {
  CLUSTER_NAME=${CLUSTER_NAME:-$(tf_output_raw cluster_name)}
  AWS_REGION=${AWS_REGION:-$(tf_output_raw aws_region)}
  AWS_ACCOUNT_ID=${AWS_ACCOUNT_ID:-$(tf_output_raw aws_account_id)}
  VPC_ID=${VPC_ID:-$(tf_output_raw vpc_id)}

  if command -v kubectl >/dev/null 2>&1; then
    CURRENT_CONTEXT=$(kubectl config current-context 2>/dev/null || true)
  fi
}

print_plan() {
  cat <<EOF
About to destroy the ScaleStyle ${TARGET_ENVIRONMENT} environment.

Target details:
  Terraform dir:  $TF_DIR
  AWS account:    ${AWS_ACCOUNT_ID:-<unavailable from terraform output>}
  AWS region:     ${AWS_REGION:-<unavailable from terraform output>}
  EKS cluster:    ${CLUSTER_NAME:-<unavailable from terraform output>}
  kubectl context:${CURRENT_CONTEXT:-<not configured>}
  App namespace:  $NAMESPACE
  Kafka namespace:$KAFKA_NAMESPACE

Planned destroy order:
  1. Delete the public ALB ingress and give the controller a chance to release AWS load balancer resources.
  2. Uninstall Helm-managed workloads that are outside the main Kustomize apply path.
  3. Delete Strimzi Kafka CRs before uninstalling the Strimzi operator.
  4. Delete the EKS app overlay resources.
  5. Run terraform destroy.

Important caveats:
  - ALB cleanup in AWS is asynchronous. The Ingress object may be gone before the ALB and its security groups disappear.
  - Kafka PVCs are configured with deleteClaim=false, so EBS-backed claims can remain unless removed intentionally.
  - S3 bucket deletion can fail if versioned objects remain.
  - ECR repositories are not force-deleted. Images usually need manual cleanup first.
EOF
}

confirm_destroy() {
  if [[ "$ASSUME_YES" == "true" ]]; then
    log_warn "--yes supplied; skipping interactive confirmation"
    return
  fi

  print_plan
  echo
  read -r -p "Type '${TARGET_ENVIRONMENT}' to confirm the target environment: " confirmed_env
  if [[ "$confirmed_env" != "$TARGET_ENVIRONMENT" ]]; then
    log_error "Environment confirmation did not match. Aborting."
    exit 1
  fi

  read -r -p "Type 'destroy-production' to continue: " confirmation_phrase
  if [[ "$confirmation_phrase" != "destroy-production" ]]; then
    log_error "Confirmation phrase did not match. Aborting."
    exit 1
  fi
}

update_kubeconfig() {
  if [[ -z "$CLUSTER_NAME" || -z "$AWS_REGION" ]]; then
    log_warn "Terraform outputs for cluster name or region are unavailable; skipping kubeconfig refresh"
    return
  fi

  log_info "Refreshing kubeconfig for cluster '$CLUSTER_NAME' in region '$AWS_REGION'"
  "$ROOT_DIR/infrastructure/terraform/update-kubeconfig.sh"
}

ensure_cluster_access() {
  if ! kubectl cluster-info >/dev/null 2>&1; then
    log_error "kubectl cannot reach the cluster. Use --skip-k8s-teardown only if the cluster is already gone."
    exit 1
  fi
}

helm_release_exists() {
  local release_name="$1"
  local namespace="$2"
  helm status "$release_name" -n "$namespace" >/dev/null 2>&1
}

uninstall_helm_release() {
  local release_name="$1"
  local namespace="$2"
  local timeout="$3"

  if helm_release_exists "$release_name" "$namespace"; then
    log_info "Uninstalling Helm release '$release_name' from namespace '$namespace'"
    helm uninstall "$release_name" -n "$namespace" --wait --timeout "$timeout"
  else
    log_info "Helm release '$release_name' not present in namespace '$namespace'; skipping"
  fi
}

delete_ingress_first() {
  local ingress_name="scalestyle-alb"

  if kubectl get ingress "$ingress_name" -n "$NAMESPACE" >/dev/null 2>&1; then
    log_info "Deleting public ingress '$ingress_name' before tearing down the rest of the app"
    kubectl delete ingress "$ingress_name" -n "$NAMESPACE" --ignore-not-found=true
    if ! kubectl wait --for=delete "ingress/${ingress_name}" -n "$NAMESPACE" --timeout="$INGRESS_DELETE_TIMEOUT"; then
      log_warn "Ingress object deletion did not finish within $INGRESS_DELETE_TIMEOUT"
      log_warn "AWS may still be tearing down the ALB and related security groups. Review the EC2 and ELB consoles if Terraform later fails on dependencies."
    fi
  else
    log_info "Ingress '$ingress_name' not present; skipping explicit ingress teardown"
  fi
}

delete_kafka_crs() {
  local kafka_overlay="$ROOT_DIR/infrastructure/k8s/overlays/eks/kafka"

  if [[ ! -d "$kafka_overlay" ]]; then
    log_warn "Kafka overlay not found at $kafka_overlay; skipping Kafka CR deletion"
    return
  fi

  log_info "Deleting Kafka CRs from the EKS Kafka overlay before uninstalling Strimzi"
  kubectl delete -k "$kafka_overlay" --ignore-not-found=true --wait=false || true

  if kubectl get kafkas.kafka.strimzi.io scalestyle-kafka -n "$KAFKA_NAMESPACE" >/dev/null 2>&1; then
    if ! kubectl wait --for=delete kafkas.kafka.strimzi.io/scalestyle-kafka -n "$KAFKA_NAMESPACE" --timeout="$KAFKA_DELETE_TIMEOUT"; then
      log_warn "Kafka CR still exists after $KAFKA_DELETE_TIMEOUT"
      log_warn "Strimzi may still be finalizing brokers or retained PVCs. Do not remove the operator until the Kafka CR is gone unless you plan to clean up finalizers manually."
    fi
  fi
}

delete_app_overlay() {
  local overlay_path="$ROOT_DIR/infrastructure/k8s/overlays/eks"
  log_info "Deleting application overlay resources"
  kubectl delete job data-init -n "$NAMESPACE" --ignore-not-found=true || true
  kubectl delete -k "$overlay_path" --ignore-not-found=true --wait=false || true
}

cleanup_alb_controller_iam() {
  local policy_name role_name policy_arn

  if [[ -z "$CLUSTER_NAME" || -z "$AWS_ACCOUNT_ID" ]]; then
    log_warn "Cluster name or AWS account ID unavailable; skipping ALB controller IAM cleanup"
    return
  fi

  policy_name="AWSLoadBalancerControllerIAMPolicy-${CLUSTER_NAME}"
  role_name="AmazonEKSLoadBalancerControllerRole-${CLUSTER_NAME}"
  policy_arn=$(aws iam list-policies --scope Local --query "Policies[?PolicyName=='${policy_name}'].Arn | [0]" --output text 2>/dev/null || echo None)

  kubectl delete serviceaccount "$ALB_CONTROLLER_SERVICE_ACCOUNT" -n "$ALB_CONTROLLER_NAMESPACE" --ignore-not-found=true >/dev/null 2>&1 || true

  if aws iam get-role --role-name "$role_name" >/dev/null 2>&1; then
    if [[ -n "$policy_arn" && "$policy_arn" != "None" ]]; then
      aws iam detach-role-policy --role-name "$role_name" --policy-arn "$policy_arn" >/dev/null 2>&1 || true
    fi

    if ! aws iam delete-role --role-name "$role_name" >/dev/null 2>&1; then
      log_warn "Unable to delete IAM role '$role_name'. Review whether other policies or sessions still reference it."
    fi
  fi

  if [[ -n "$policy_arn" && "$policy_arn" != "None" ]]; then
    if ! aws iam delete-policy --policy-arn "$policy_arn" >/dev/null 2>&1; then
      log_warn "Unable to delete IAM policy '$policy_arn'. It may still be attached elsewhere."
    fi
  fi
}

report_residual_storage() {
  local pvc_output
  pvc_output=$(kubectl get pvc -A --no-headers 2>/dev/null | awk '$1 == "'$NAMESPACE'" || $1 == "'$KAFKA_NAMESPACE'" {print}' || true)
  if [[ -n "$pvc_output" ]]; then
    log_warn "PersistentVolumeClaims still present in '$NAMESPACE' or '$KAFKA_NAMESPACE':"
    echo "$pvc_output"
    log_warn "Review these claims before or after Terraform destroy. Kafka PVCs are configured with deleteClaim=false, and PVC-backed EBS volumes may linger until claims and PVs are removed."
  fi
}

destroy_kubernetes_layer() {
  update_kubeconfig
  ensure_cluster_access
  delete_ingress_first
  uninstall_helm_release "$MILVUS_RELEASE" "$NAMESPACE" 20m || true
  delete_kafka_crs
  uninstall_helm_release "$STRIMZI_RELEASE" "$KAFKA_NAMESPACE" 10m || true
  uninstall_helm_release "$ALB_CONTROLLER_RELEASE" "$ALB_CONTROLLER_NAMESPACE" 10m || true
  cleanup_alb_controller_iam
  delete_app_overlay
  report_residual_storage
}

destroy_terraform_layer() {
  log_info "Running terraform destroy in $TF_DIR"
  terraform -chdir="$TF_DIR" destroy "${TERRAFORM_ARGS[@]}"
}

print_post_destroy_caveats() {
  cat <<EOF

Post-destroy review checklist:
  - ALB and security groups: AWS may take additional time to remove load balancer dependencies after Ingress deletion.
  - PVC/EBS: inspect Kubernetes PVCs and EBS volumes, especially Kafka volumes retained by deleteClaim=false.
  - S3: empty versioned buckets if terraform destroy reports bucket-not-empty failures.
  - ECR: delete remaining images if repository deletion is blocked.
  - IAM: if the ALB controller IAM role or policy could not be deleted, review them manually.
EOF
}

main() {
  parse_args "$@"
  validate_dependencies
  load_context
  confirm_destroy

  if [[ "$SKIP_K8S_TEARDOWN" != "true" ]]; then
    destroy_kubernetes_layer
  else
    log_warn "Skipping Kubernetes teardown at operator request"
  fi

  if [[ "$SKIP_TERRAFORM_DESTROY" != "true" ]]; then
    destroy_terraform_layer
  else
    log_warn "Skipping Terraform destroy at operator request"
  fi

  print_post_destroy_caveats
}

main "$@"