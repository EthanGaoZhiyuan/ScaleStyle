#!/usr/bin/env bash

set -euo pipefail

TF_DIR=${TF_DIR:-infrastructure/terraform}
NAMESPACE=${NAMESPACE:-scalestyle}
TEST_USER=${TEST_USER:-demo_user}
TEST_ITEM=${TEST_ITEM:-0108775051}

redis_host=$(terraform -chdir="$TF_DIR" output -raw redis_primary_endpoint)
alb_hostname=$(kubectl get ingress scalestyle-alb -n "$NAMESPACE" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')

echo "=== Cluster workloads ==="
kubectl get pods -A
kubectl get svc -n "$NAMESPACE"
kubectl get ingress -n "$NAMESPACE"

echo "=== Deployment health ==="
kubectl wait --for=condition=Available deployment/gateway -n "$NAMESPACE" --timeout=10m
kubectl wait --for=condition=Available deployment/inference -n "$NAMESPACE" --timeout=10m
kubectl wait --for=condition=Available deployment/event-consumer-primary -n "$NAMESPACE" --timeout=10m
kubectl wait --for=condition=Available deployment/event-consumer-retry -n "$NAMESPACE" --timeout=10m

echo "=== Internal connectivity ==="
kubectl run eks-netcheck --rm -i --restart=Never -n "$NAMESPACE" --image=busybox:1.36 -- sh -c "nc -z inference 8000 && nc -z milvus 19530 && nc -z scalestyle-kafka-kafka-bootstrap.kafka-system.svc.cluster.local 9093 && nc -z ${redis_host} 6379"

echo "=== Public endpoints ==="
curl -fsS "http://${alb_hostname}/search?query=dress&k=3" | head -c 500 && echo
curl -fsS -X POST "http://${alb_hostname}/search/image" -H 'Content-Type: application/json' -d '{"mode":"text_to_image","query":"red dress","k":3}' | head -c 500 && echo
curl -fsS -X POST "http://${alb_hostname}/events/click" -H 'Content-Type: application/json' -d "{\"event_id\":\"smoke-$(date +%s)\",\"user_id\":\"${TEST_USER}\",\"item_id\":\"${TEST_ITEM}\",\"source\":\"search\",\"session_id\":\"smoke-session\",\"query\":\"dress\",\"position\":1}" | head -c 500 && echo

sleep 5

echo "=== Consumer logs (Primary) ==="
kubectl logs deployment/event-consumer-primary -n "$NAMESPACE" --tail=50

echo "=== Consumer logs (Retry) ==="
kubectl logs deployment/event-consumer-retry -n "$NAMESPACE" --tail=50

echo "=== Redis state ==="
kubectl run eks-redischeck --rm -i --restart=Never -n "$NAMESPACE" --image=redis:7.2-alpine -- redis-cli -h "$redis_host" LRANGE "user:${TEST_USER}:recent_clicks" 0 5

echo "ALB hostname: ${alb_hostname}"