#!/usr/bin/env bash

set -euo pipefail

TF_DIR=${TF_DIR:-infrastructure/terraform}

if ! command -v terraform >/dev/null 2>&1; then
  echo "terraform is required" >&2
  exit 1
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI is required" >&2
  exit 1
fi

cluster_name=${CLUSTER_NAME:-$(terraform -chdir="$TF_DIR" output -raw cluster_name)}
aws_region=${AWS_REGION:-$(terraform -chdir="$TF_DIR" output -raw aws_region)}

aws eks update-kubeconfig --region "$aws_region" --name "$cluster_name"
kubectl get nodes