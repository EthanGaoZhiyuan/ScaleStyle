#!/usr/bin/env bash
# sync-ecr-images.sh — inject ECR repository URLs and the immutable image tag into
# the EKS kustomization overlay before applying manifests.
#
# Required env vars:
#   IMAGE_TAG  — immutable tag to deploy (e.g. "git-3f8c9ab").
#                Must match the tag pushed by build-push-ecr.sh.
#                Do NOT pass "latest" — the placeholder "git-COMMIT_SHA" in
#                kustomization.yaml is a deliberate guard that will fail loudly
#                if you forget to set IMAGE_TAG.
#
# Usage:
#   IMAGE_TAG=git-$(git rev-parse --short HEAD) ./sync-ecr-images.sh

set -euo pipefail

TF_DIR=${TF_DIR:-infrastructure/terraform}
KUSTOMIZATION_FILE=${KUSTOMIZATION_FILE:-infrastructure/k8s/overlays/eks/kustomization.yaml}

if [[ -z "${IMAGE_TAG:-}" ]]; then
  echo "ERROR: IMAGE_TAG is required and must not be empty." >&2
  echo "  Set it to the git-SHA tag built by build-push-ecr.sh, e.g.:" >&2
  echo "    IMAGE_TAG=git-\$(git rev-parse --short HEAD) $0" >&2
  exit 1
fi

if [[ "$IMAGE_TAG" == "latest" ]]; then
  echo "ERROR: IMAGE_TAG=latest is not allowed in production." >&2
  echo "  Use an immutable tag, e.g.: IMAGE_TAG=git-\$(git rev-parse --short HEAD)" >&2
  exit 1
fi

if ! command -v terraform >/dev/null 2>&1; then
  echo "terraform is required" >&2
  exit 1
fi

gateway_repo=$(terraform -chdir="$TF_DIR" output -raw gateway_service_repository_url)
inference_repo=$(terraform -chdir="$TF_DIR" output -raw inference_service_repository_url)
event_consumer_repo=$(terraform -chdir="$TF_DIR" output -raw event_consumer_repository_url)
data_init_repo=$(terraform -chdir="$TF_DIR" output -raw data_init_repository_url)

# Replace ECR repository URLs (newName fields).
sed -i.bak \
  -e "s|[A-Za-z0-9<>._/-]*gateway-service|${gateway_repo}|g" \
  -e "s|[A-Za-z0-9<>._/-]*inference-service|${inference_repo}|g" \
  -e "s|[A-Za-z0-9<>._/-]*data-init|${data_init_repo}|g" \
  -e "s|[A-Za-z0-9<>._/-]*event-consumer|${event_consumer_repo}|g" \
  "$KUSTOMIZATION_FILE"

# Replace the tag placeholder with the actual immutable tag.
# The placeholder "git-COMMIT_SHA" is intentionally non-runnable so that
# applying without this step fails immediately with an ECR 404 rather than
# silently pulling an old image from a different node's cache.
sed -i.bak \
  -e "s|\"git-COMMIT_SHA\"|\"${IMAGE_TAG}\"|g" \
  "$KUSTOMIZATION_FILE"

rm -f "${KUSTOMIZATION_FILE}.bak"

echo "Updated EKS image references: repos from Terraform, tag=${IMAGE_TAG}"
