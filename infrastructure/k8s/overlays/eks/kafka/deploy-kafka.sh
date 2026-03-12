#!/usr/bin/env bash

set -euo pipefail

KAFKA_NAMESPACE=${KAFKA_NAMESPACE:-kafka-system}
KAFKA_NAME=${KAFKA_NAME:-scalestyle-kafka}
KAFKA_OVERLAY=${KAFKA_OVERLAY:-infrastructure/k8s/overlays/eks/kafka}

kubectl apply -k "$KAFKA_OVERLAY"

kubectl wait kafka/$KAFKA_NAME \
  -n "$KAFKA_NAMESPACE" \
  --for=condition=Ready \
  --timeout=20m

kubectl wait kafkatopic/scalestyle-clicks \
  -n "$KAFKA_NAMESPACE" \
  --for=condition=Ready \
  --timeout=300s || true

kubectl get kafka -n "$KAFKA_NAMESPACE"
kubectl get kafkatopic -n "$KAFKA_NAMESPACE"