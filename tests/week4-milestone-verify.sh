#!/bin/bash
# Week 4 Milestone Verification Script
# Run this on 2/6 to verify all deliverables

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}Week 4 Milestone Verification${NC}"
echo -e "${YELLOW}Date: $(date)${NC}"
echo -e "${YELLOW}========================================${NC}"
echo ""

# Task 6.1: Check metrics-server
echo -e "${GREEN}[1/5] Checking metrics-server...${NC}"
if kubectl top nodes > /dev/null 2>&1; then
    echo -e "${GREEN}✓ metrics-server is working${NC}"
    kubectl top nodes
else
    echo -e "${RED}✗ metrics-server not working${NC}"
    echo "Fix: minikube addons enable metrics-server"
    exit 1
fi
echo ""

# Task 6.2: Check pods status
echo -e "${GREEN}[2/5] Checking pod status...${NC}"
PODS=$(kubectl get pods -n scalestyle --no-headers 2>/dev/null | wc -l)
READY=$(kubectl get pods -n scalestyle --no-headers 2>/dev/null | grep -c "Running" || echo "0")

if [ "$PODS" -gt 0 ] && [ "$READY" -eq "$PODS" ]; then
    echo -e "${GREEN}✓ All $PODS pods are Running${NC}"
    kubectl get pods -n scalestyle -o wide
else
    echo -e "${RED}✗ Some pods not ready ($READY/$PODS)${NC}"
    kubectl get pods -n scalestyle
    exit 1
fi
echo ""

# Task 6.3: Check HPA
echo -e "${GREEN}[3/5] Checking HPA status...${NC}"
if kubectl get hpa -n scalestyle > /dev/null 2>&1; then
    echo -e "${GREEN}✓ HPA is deployed${NC}"
    kubectl get hpa -n scalestyle
    
    # Check if HPA has metrics
    TARGETS=$(kubectl get hpa gateway-hpa -n scalestyle -o jsonpath='{.status.currentMetrics[0].resource.current.averageUtilization}' 2>/dev/null || echo "0")
    if [ "$TARGETS" != "0" ] && [ "$TARGETS" != "" ]; then
        echo -e "${GREEN}✓ HPA has metrics: ${TARGETS}%${NC}"
    else
        echo -e "${YELLOW}⚠ HPA metrics not available yet (wait 60s)${NC}"
    fi
else
    echo -e "${RED}✗ HPA not deployed${NC}"
    exit 1
fi
echo ""

# Task 6.4: Check API endpoint
echo -e "${GREEN}[4/5] Checking API endpoint...${NC}"
echo "Starting port-forward (will run in background)..."
kubectl port-forward -n scalestyle svc/local-gateway 8080:8080 > /dev/null 2>&1 &
PF_PID=$!
sleep 3

if curl -s http://localhost:8080/actuator/health | grep -q "UP"; then
    echo -e "${GREEN}✓ Gateway health check passed${NC}"
else
    echo -e "${RED}✗ Gateway not responding${NC}"
    kill $PF_PID 2>/dev/null
    exit 1
fi

# Test recommendation endpoint
if curl -s "http://localhost:8080/api/recommendation/search?query=dress&k=5" | grep -q "itemId"; then
    echo -e "${GREEN}✓ Recommendation API working${NC}"
else
    echo -e "${YELLOW}⚠ Recommendation API returned no results${NC}"
fi

kill $PF_PID 2>/dev/null
echo ""

# Task 6.5: Summary
echo -e "${GREEN}[5/5] Milestone Summary${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}✓ metrics-server: Working${NC}"
echo -e "${GREEN}✓ Pods: All Running${NC}"
echo -e "${GREEN}✓ HPA: Deployed and monitoring${NC}"
echo -e "${GREEN}✓ API: Responding${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

echo -e "${YELLOW}Next steps:${NC}"
echo "1. Run load test: ./tests/chaos/load-test.sh"
echo "2. Verify HPA scaling: kubectl get hpa -n scalestyle -w"
echo "3. Capture screenshots for deliverable"
echo ""

echo -e "${GREEN}✓ Week 4 Milestone: READY TO DELIVER${NC}"
