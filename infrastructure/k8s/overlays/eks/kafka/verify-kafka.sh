#!/usr/bin/env bash

set -euo pipefail

KAFKA_NAMESPACE=${KAFKA_NAMESPACE:-kafka-system}
KAFKA_NAME=${KAFKA_NAME:-scalestyle-kafka}
TOPIC_NAME=${TOPIC_NAME:-scalestyle.clicks}

kubectl config current-context
kubectl get nodes
kubectl get pods -n "$KAFKA_NAMESPACE"
kubectl get kafka -n "$KAFKA_NAMESPACE"

if [[ "${TOPIC_ONLY:-0}" == "1" ]]; then
  kubectl get kafkatopic -n "$KAFKA_NAMESPACE"
else
  kubectl wait deployment/strimzi-cluster-operator -n "$KAFKA_NAMESPACE" --for=condition=Available --timeout=300s
  kubectl wait kafka/$KAFKA_NAME -n "$KAFKA_NAMESPACE" --for=condition=Ready --timeout=20m
  kubectl get kafkatopic -n "$KAFKA_NAMESPACE"
fi

topic_found=0
while IFS= read -r topic_name; do
  if [[ "$topic_name" == "$TOPIC_NAME" ]]; then
    topic_found=1
    break
  fi
done < <(kubectl get kafkatopic -n "$KAFKA_NAMESPACE" -o jsonpath='{range .items[*]}{.spec.topicName}{"\n"}{end}')

if [[ "$topic_found" -ne 1 ]]; then
  echo "Kafka topic not found: $TOPIC_NAME" >&2
  exit 1
fi

echo "Kafka topic verified via Strimzi CR: $TOPIC_NAME"
echo "Kafka bootstrap service: ${KAFKA_NAME}-kafka-bootstrap.${KAFKA_NAMESPACE}.svc.cluster.local:9093 (TLS + SCRAM)"