#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/../../../../" && pwd)
TF_DIR=${TF_DIR:-$ROOT_DIR/infrastructure/terraform}
NAMESPACE=${NAMESPACE:-scalestyle}
SKIP_DATA_INIT=${SKIP_DATA_INIT:-false}

echo "[1/10] Apply Terraform (adds ElastiCache if needed)"
terraform -chdir="$TF_DIR" apply "$@"

echo "[2/10] Update kubeconfig"
"$ROOT_DIR/infrastructure/terraform/update-kubeconfig.sh"

echo "[3/10] Sync ECR image URLs"
"$ROOT_DIR/infrastructure/k8s/overlays/eks/sync-ecr-images.sh"

echo "[4/10] Enforce immutable image policy"
if ! python - "$ROOT_DIR" <<'PY'
import pathlib
import re
import sys
root = pathlib.Path(sys.argv[1])
pattern = re.compile(r"^\s*image:\s*.+:latest\s*$")
bad = []
for base in (root / "infrastructure/k8s/base", root / "infrastructure/k8s/overlays/eks"):
    for p in base.rglob("*.y*ml"):
        for i, line in enumerate(p.read_text().splitlines(), start=1):
            if pattern.search(line):
                bad.append((p, i, line.strip()))
if bad:
    for p, i, line in bad:
        print(f"{p}:{i}:{line}")
    sys.exit(1)
PY
then
  echo "ERROR: Found disallowed :latest image reference in Kubernetes manifests." >&2
  exit 1
fi
if ! kubectl kustomize "$ROOT_DIR/infrastructure/k8s/overlays/eks" | python -c '
import re
import sys
data = sys.stdin.read().splitlines()
bad = [
    (i + 1, line) for i, line in enumerate(data)
    if "__MUST_BE_OVERRIDDEN_BY_KUSTOMIZE__" in line
    or re.search(r"^\s*image:\s*.+:latest\s*$", line)
]
if bad:
    for i, line in bad:
        print(f"rendered:{i}:{line}")
    sys.exit(1)
'
then
  echo "ERROR: Found unresolved image override placeholders in EKS overlay manifests." >&2
  exit 1
fi

echo "[5/10] Install ALB controller"
"$ROOT_DIR/infrastructure/k8s/overlays/eks/install-alb-controller.sh"

echo "[6/10] Verify Kafka"
"$ROOT_DIR/infrastructure/k8s/overlays/eks/kafka/verify-kafka.sh"

echo "[7/10] Apply cloud config"
"$ROOT_DIR/infrastructure/k8s/overlays/eks/apply-cloud-config.sh"

echo "[8/10] Deploy Milvus"
"$ROOT_DIR/infrastructure/k8s/overlays/eks/deploy-milvus.sh"

echo "[9/10] Apply application manifests"
kubectl delete job data-init -n "$NAMESPACE" --ignore-not-found=true
kubectl apply -k "$ROOT_DIR/infrastructure/k8s/overlays/eks"

echo "[10/10] Wait for workloads"
kubectl wait --for=condition=Available deployment/gateway -n "$NAMESPACE" --timeout=15m
kubectl wait --for=condition=Available deployment/inference -n "$NAMESPACE" --timeout=20m
kubectl wait --for=condition=Available deployment/event-consumer-primary -n "$NAMESPACE" --timeout=10m
kubectl wait --for=condition=Available deployment/event-consumer-retry -n "$NAMESPACE" --timeout=10m

if [[ "$SKIP_DATA_INIT" == "true" ]]; then
  echo "SKIP_DATA_INIT=true -> skipping data-init completion wait"
else
  if ! kubectl wait --for=condition=complete job/data-init -n "$NAMESPACE" --timeout=30m; then
    echo "ERROR: data-init job did not complete successfully" >&2
    kubectl describe job data-init -n "$NAMESPACE" || true
    kubectl logs job/data-init -n "$NAMESPACE" --all-containers=true || true
    exit 1
  fi
fi

alb_hostname=$(kubectl get ingress scalestyle-alb -n "$NAMESPACE" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
echo "ALB hostname: ${alb_hostname:-pending}"
echo "Search endpoint: http://${alb_hostname:-<pending>}/search?query=dress&k=5"
echo "Image search endpoint: http://${alb_hostname:-<pending>}/search/image"
echo "Click endpoint: http://${alb_hostname:-<pending>}/events/click"