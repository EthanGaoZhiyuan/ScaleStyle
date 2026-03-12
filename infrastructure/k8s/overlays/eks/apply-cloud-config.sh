#!/usr/bin/env bash

set -euo pipefail

TF_DIR=${TF_DIR:-infrastructure/terraform}
NAMESPACE=${NAMESPACE:-scalestyle}
KAFKA_NAMESPACE=${KAFKA_NAMESPACE:-kafka-system}

b64decode() {
  if base64 --decode >/dev/null 2>&1 <<<'dGVzdA=='; then
    base64 --decode
  else
    base64 -D
  fi
}

redis_host=$(terraform -chdir="$TF_DIR" output -raw redis_primary_endpoint)
redis_port=$(terraform -chdir="$TF_DIR" output -raw redis_port)
aws_region=$(terraform -chdir="$TF_DIR" output -raw aws_region)
s3_bucket=$(terraform -chdir="$TF_DIR" output -raw s3_bucket_name)

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

# scalestyle-cloud-config is mounted AFTER scalestyle-config in every pod's
# envFrom list, so any key set here overrides the base ConfigMap value.
# Keep the OTel exporter settings in sync with kustomization.yaml.
kubectl create configmap scalestyle-cloud-config \
  -n "$NAMESPACE" \
  --from-literal=ENVIRONMENT=eks-production \
  --from-literal=AWS_REGION="$aws_region" \
  --from-literal=REDIS_HOST="$redis_host" \
  --from-literal=REDIS_PORT="$redis_port" \
  --from-literal=REDIS_TLS="true" \
  --from-literal=MILVUS_HOST=milvus.scalestyle.svc.cluster.local \
  --from-literal=MILVUS_PORT=19530 \
  --from-literal=MILVUS_COLLECTION=scale_style_bge_v2 \
  --from-literal=MILVUS_IMAGE_COLLECTION=scalestyle_image_v1 \
  --from-literal=INFERENCE_BASE_URL=http://inference.scalestyle.svc.cluster.local:8000 \
  --from-literal=SPRING_KAFKA_BOOTSTRAP_SERVERS=scalestyle-kafka-kafka-bootstrap.kafka-system.svc.cluster.local:9093 \
  --from-literal=KAFKA_BOOTSTRAP_SERVERS=scalestyle-kafka-kafka-bootstrap.kafka-system.svc.cluster.local:9093 \
  --from-literal=KAFKA_SECURITY_PROTOCOL=SASL_SSL \
  --from-literal=KAFKA_SASL_MECHANISM=SCRAM-SHA-512 \
  --from-literal=KAFKA_POLL_MAX_RECORDS=100 \
  --from-literal=KAFKA_TOPIC=scalestyle.clicks \
  --from-literal=KAFKA_GROUP_ID=event-consumer \
  --from-literal=OTEL_TRACES_EXPORTER=otlp \
  --from-literal=OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger-collector.scalestyle.svc.cluster.local:4317 \
  --from-literal=OTEL_METRICS_EXPORTER=none \
  --from-literal=OTEL_LOGS_EXPORTER=none \
  --from-literal=S3_BUCKET="$s3_bucket" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl get configmap scalestyle-cloud-config -n "$NAMESPACE"

# Copy Strimzi-generated SCRAM credentials into application namespace so
# workloads can mount usernames/passwords without cross-namespace secret reads.
gateway_user=$(kubectl get secret gateway-producer -n "$KAFKA_NAMESPACE" -o jsonpath='{.data.username}' | b64decode)
gateway_password=$(kubectl get secret gateway-producer -n "$KAFKA_NAMESPACE" -o jsonpath='{.data.password}' | b64decode)
consumer_primary_user=$(kubectl get secret event-consumer-primary -n "$KAFKA_NAMESPACE" -o jsonpath='{.data.username}' | b64decode)
consumer_primary_password=$(kubectl get secret event-consumer-primary -n "$KAFKA_NAMESPACE" -o jsonpath='{.data.password}' | b64decode)
consumer_retry_user=$(kubectl get secret event-consumer-retry -n "$KAFKA_NAMESPACE" -o jsonpath='{.data.username}' | b64decode)
consumer_retry_password=$(kubectl get secret event-consumer-retry -n "$KAFKA_NAMESPACE" -o jsonpath='{.data.password}' | b64decode)
tmp_ca_file=$(mktemp)
trap 'rm -f "$tmp_ca_file"' EXIT
kubectl get secret scalestyle-kafka-cluster-ca-cert -n "$KAFKA_NAMESPACE" -o jsonpath='{.data.ca\.crt}' | b64decode > "$tmp_ca_file"

kubectl create secret generic gateway-kafka-auth \
  -n "$NAMESPACE" \
  --from-literal=KAFKA_USERNAME="$gateway_user" \
  --from-literal=KAFKA_PASSWORD="$gateway_password" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic event-consumer-primary-kafka-auth \
  -n "$NAMESPACE" \
  --from-literal=KAFKA_USERNAME="$consumer_primary_user" \
  --from-literal=KAFKA_PASSWORD="$consumer_primary_password" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic event-consumer-retry-kafka-auth \
  -n "$NAMESPACE" \
  --from-literal=KAFKA_USERNAME="$consumer_retry_user" \
  --from-literal=KAFKA_PASSWORD="$consumer_retry_password" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic kafka-cluster-ca \
  -n "$NAMESPACE" \
  --from-file=ca.crt="$tmp_ca_file" \
  --dry-run=client -o yaml | kubectl apply -f -