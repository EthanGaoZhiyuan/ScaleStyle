#!/usr/bin/env bash

set -euo pipefail

STRIMZI_VERSION=${STRIMZI_VERSION:-0.43.0}
KAFKA_NAMESPACE=${KAFKA_NAMESPACE:-kafka-system}

if ! command -v helm >/dev/null 2>&1; then
  echo "helm is required" >&2
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl is required" >&2
  exit 1
fi

kubectl get namespace "$KAFKA_NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$KAFKA_NAMESPACE"

helm repo add strimzi https://strimzi.io/charts/ >/dev/null 2>&1 || true
helm repo update >/dev/null

helm upgrade --install strimzi-kafka-operator strimzi/strimzi-kafka-operator \
  --namespace "$KAFKA_NAMESPACE" \
  --version "$STRIMZI_VERSION" \
  --set watchAnyNamespace=false \
  --set watchNamespaces[0]="$KAFKA_NAMESPACE" \
  --wait \
  --timeout 10m

kubectl rollout status deployment/strimzi-cluster-operator -n "$KAFKA_NAMESPACE" --timeout=300s