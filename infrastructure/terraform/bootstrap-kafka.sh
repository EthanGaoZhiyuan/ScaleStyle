#!/usr/bin/env bash
# bootstrap-kafka.sh — Full EKS + Strimzi Kafka bootstrap
#
# Kafka architecture: Strimzi-managed on EKS (NOT Amazon MSK)
# ─────────────────────────────────────────────────────────────
# ScaleStyle runs a self-managed Kafka cluster using the Strimzi CNCF operator.
# Strimzi deploys and manages Kafka brokers, topics, and users as Kubernetes
# custom resources (kafka.strimzi.io/v1beta2).
#
# This means:
#   - No aws_msk_* Terraform resources — Kafka is outside the Terraform layer.
#   - Broker lifecycle is owned by the Strimzi Cluster Operator (Helm-installed).
#   - Topics are declared as KafkaTopic CRs in k8s/overlays/eks/kafka/.
#   - Bootstrap: scalestyle-kafka-kafka-bootstrap.kafka-system.svc.cluster.local:9092
#
# Why Strimzi over MSK:
#   + Full broker config control (log compaction, exactly-once, custom listeners)
#   + No per-broker hourly pricing (MSK charges per broker/hour regardless of load)
#   + KafkaTopic CRs integrate cleanly with the existing Kustomize GitOps flow
# Trade-offs vs MSK:
#   - No AWS-managed HA failover (Strimzi handles this via ZooKeeper/KRaft)
#   - Manual broker version upgrades
#   - Operator pods consume ~200m CPU / 512Mi RAM in kafka-system ns
#
# Steps performed by this script:
#   1. terraform apply — provisions EKS, ElastiCache, ECR, IAM, VPC
#   2. update-kubeconfig.sh — syncs kubeconfig for the new cluster
#   3. sync-ecr-images.sh  — injects ECR URLs into the EKS Kustomize overlay
#   4. install-strimzi.sh  — installs the Strimzi operator via Helm
#   5. deploy-kafka.sh     — applies Kafka CR + KafkaTopic CRs
#   6. verify-kafka.sh     — smoke-tests broker readiness and topic existence

set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/../.." && pwd)
TF_DIR=${TF_DIR:-$ROOT_DIR/infrastructure/terraform}

if ! command -v terraform >/dev/null 2>&1; then
  echo "terraform is required" >&2
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl is required" >&2
  exit 1
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI is required" >&2
  exit 1
fi

if ! command -v helm >/dev/null 2>&1; then
  echo "helm is required" >&2
  exit 1
fi

echo "[1/6] terraform apply"
terraform -chdir="$TF_DIR" apply "$@"

echo "[2/6] update kubeconfig"
"$ROOT_DIR/infrastructure/terraform/update-kubeconfig.sh"

echo "[3/6] sync ECR image URLs into EKS overlay"
"$ROOT_DIR/infrastructure/k8s/overlays/eks/sync-ecr-images.sh"

echo "[4/6] install Strimzi operator"
"$ROOT_DIR/infrastructure/k8s/overlays/eks/kafka/install-strimzi.sh"

echo "[5/6] deploy Kafka cluster and topic"
"$ROOT_DIR/infrastructure/k8s/overlays/eks/kafka/deploy-kafka.sh"

echo "[6/6] verify Kafka"
"$ROOT_DIR/infrastructure/k8s/overlays/eks/kafka/verify-kafka.sh"