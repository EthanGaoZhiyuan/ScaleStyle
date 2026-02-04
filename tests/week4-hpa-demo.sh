#!/bin/bash
# Week 4 HPA Demo Script - 2/6 Milestone
# Purpose: Step-by-step guide to demonstrate K8s stability + HPA scaling

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Week 4 HPA Demo - Step by Step ===${NC}\n"

# Step 1: Deploy to Minikube
echo -e "${YELLOW}Step 1: Deploying to Minikube...${NC}"
cd /Users/Ethan/Develop/ScaleStyle
make deploy-minikube

echo -e "\n${YELLOW}Waiting for pods to be ready (this may take 2-3 minutes)...${NC}"
kubectl wait --for=condition=Ready pods --all -n scalestyle --timeout=300s || true

# Step 2: Verify Metrics Server
echo -e "\n${YELLOW}Step 2: Verifying Metrics Server...${NC}"
kubectl get apiservices | grep metrics
kubectl top nodes || echo "If metrics-server not found, run: minikube addons enable metrics-server"

# Wait for metrics to be available
echo -e "\n${YELLOW}Waiting for pod metrics (30 seconds)...${NC}"
sleep 30

# Step 3: Check Initial State
echo -e "\n${YELLOW}Step 3: Checking initial state...${NC}"
echo "=== HPA Status ==="
kubectl get hpa -n scalestyle

echo -e "\n=== Gateway Deployment ==="
kubectl get deploy gateway -n scalestyle

echo -e "\n=== Pod Resource Usage ==="
kubectl top pods -n scalestyle

# Step 4: Expose Service
echo -e "\n${YELLOW}Step 4: Setting up port forwarding...${NC}"
echo "Run this in a separate terminal:"
echo "  kubectl port-forward -n scalestyle svc/gateway 8080:8080"
echo ""
echo "Then test with:"
echo "  curl http://localhost:8080/api/recommendation/search?query=dress&k=5"
echo ""

# Step 5: Load Test Instructions
echo -e "${YELLOW}Step 5: Start load test (choose one method):${NC}\n"

echo "=== Method A: Using 'hey' (recommended) ==="
echo "  # Install: brew install hey"
echo "  hey -z 3m -c 50 -q 10 'http://localhost:8080/api/recommendation/search?query=dress&k=5'"
echo ""

echo "=== Method B: Using ab (ApacheBench) ==="
echo "  ab -n 10000 -c 50 -t 180 'http://localhost:8080/api/recommendation/search?query=dress&k=5'"
echo ""

echo "=== Method C: Simple curl loop ==="
echo "  for i in {1..1000}; do curl -s 'http://localhost:8080/api/recommendation/search?query=dress&k=5' > /dev/null & done"
echo ""

# Step 6: Monitoring Commands
echo -e "${YELLOW}Step 6: Watch HPA scaling (run in separate terminal):${NC}\n"
echo "  # Terminal 1: Watch HPA status"
echo "  kubectl get hpa -n scalestyle -w"
echo ""
echo "  # Terminal 2: Watch pod replicas"
echo "  kubectl get pods -n scalestyle -l app.kubernetes.io/name=gateway -w"
echo ""
echo "  # Terminal 3: Watch resource usage"
echo "  watch -n 2 'kubectl top pods -n scalestyle'"
echo ""

# Step 7: Evidence Collection
echo -e "${YELLOW}Step 7: Collect evidence (screenshots + logs):${NC}\n"

echo "📸 Screenshot checklist:"
echo "  1. kubectl get hpa -n scalestyle    (showing TARGETS like 45%/40% or higher)"
echo "  2. kubectl get pods -n scalestyle   (showing 4+ gateway pods)"
echo "  3. Load test output                 (showing requests/sec, latency)"
echo "  4. kubectl top pods -n scalestyle   (showing CPU usage)"
echo ""

echo "📝 Save logs:"
echo "  kubectl get hpa -n scalestyle -o yaml > evidence/hpa-config.yaml"
echo "  kubectl get events -n scalestyle --sort-by='.lastTimestamp' > evidence/events.log"
echo "  kubectl describe hpa gateway-hpa -n scalestyle > evidence/hpa-describe.txt"
echo ""

# Step 8: Expected Results
echo -e "${YELLOW}Expected Results:${NC}\n"
echo "✓ Initial state: 2 gateway pods"
echo "✓ Under load: CPU usage rises above 40%"
echo "✓ HPA triggers: Scales to 3-5 pods within 30-60 seconds"
echo "✓ System stable: API returns 200, no errors"
echo "✓ Scale down: After load stops, gradually returns to 2 pods (5 minutes)"
echo ""

echo -e "${GREEN}=== Setup Complete! ===${NC}"
echo "Follow the steps above to demonstrate HPA scaling."
echo ""
echo "Quick reference:"
echo "  - Run verification: ./tests/week4-milestone-verify.sh"
echo "  - Check logs: kubectl logs -n scalestyle -l app.kubernetes.io/name=gateway"
echo "  - Delete deployment: make k8s-delete-minikube"
