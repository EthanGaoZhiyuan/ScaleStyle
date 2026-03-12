#!/usr/bin/env bash

set -euo pipefail

NAMESPACE=${NAMESPACE:-scalestyle}
VALUES_FILE=${VALUES_FILE:-infrastructure/k8s/helm-values/milvus-standalone.yaml}

kubectl apply -f infrastructure/k8s/overlays/eks/storage-class-ebs.yaml
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

helm repo add milvus https://zilliztech.github.io/milvus-helm/ >/dev/null 2>&1 || true
helm repo update >/dev/null

helm upgrade --install milvus milvus/milvus \
  --namespace "$NAMESPACE" \
  -f "$VALUES_FILE" \
  --wait \
  --timeout 20m

kubectl get pods -n "$NAMESPACE" -l app.kubernetes.io/instance=milvus
kubectl get svc -n "$NAMESPACE" | grep milvus || true