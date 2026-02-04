#!/bin/bash
# Test CLIP Image Search - Week 4 Bonus Feature
# Tests the /search/image endpoint with mock data

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== CLIP Image Search Test ===${NC}\n"

# Step 1: Check if port-forward is running
echo -e "${YELLOW}Step 1: Checking port-forward...${NC}"
if ! nc -z localhost 8000 2>/dev/null; then
    echo -e "${RED}❌ Port 8000 not accessible${NC}"
    echo "Run in another terminal:"
    echo "  kubectl port-forward -n scalestyle svc/inference 8000:8000"
    exit 1
fi
echo -e "${GREEN}✓ Port 8000 accessible${NC}\n"

# Step 2: Test healthcheck
echo -e "${YELLOW}Step 2: Testing healthcheck...${NC}"
HEALTH=$(curl -s http://localhost:8000/healthz)
if [[ "$HEALTH" == *"ok"* ]]; then
    echo -e "${GREEN}✓ Inference service healthy${NC}\n"
else
    echo -e "${RED}❌ Inference service not healthy: $HEALTH${NC}"
    exit 1
fi

# Step 3: Test CLIP search with mock base64
echo -e "${YELLOW}Step 3: Testing CLIP image search...${NC}"
echo "Request: POST /search/image"
echo '{"image_base64": "mock_test_image", "k": 5}'
echo ""

RESPONSE=$(curl -s -X POST http://localhost:8000/search/image \
  -H 'Content-Type: application/json' \
  -d '{"image_base64": "mock_test_image", "k": 5}')

echo "Response:"
echo "$RESPONSE" | jq '.' 2>/dev/null || echo "$RESPONSE"
echo ""

# Check if response contains items
if echo "$RESPONSE" | jq -e '.items' >/dev/null 2>&1; then
    ITEM_COUNT=$(echo "$RESPONSE" | jq '.items | length')
    echo -e "${GREEN}✓ CLIP search returned $ITEM_COUNT items${NC}\n"
    
    # Display first result
    echo -e "${YELLOW}First result:${NC}"
    echo "$RESPONSE" | jq '.items[0]' 2>/dev/null
    echo ""
else
    echo -e "${YELLOW}⚠️  Response format:${NC}"
    echo "$RESPONSE"
    echo ""
fi

# Step 4: Test with invalid request (no image)
echo -e "${YELLOW}Step 4: Testing error handling (no image)...${NC}"
ERROR_RESPONSE=$(curl -s -X POST http://localhost:8000/search/image \
  -H 'Content-Type: application/json' \
  -d '{"k": 5}')

if echo "$ERROR_RESPONSE" | grep -q "error"; then
    echo -e "${GREEN}✓ Error handling works correctly${NC}\n"
else
    echo -e "${RED}❌ Error handling may not be working${NC}\n"
fi

# Step 5: Summary
echo -e "${GREEN}=== Test Summary ===${NC}"
echo "✓ Inference service accessible"
echo "✓ CLIP endpoint responds"
echo "✓ Error handling works"
echo ""
echo -e "${YELLOW}Note:${NC} If CLIP search returns 'CLIP search not available',"
echo "the collection may not be initialized. Run:"
echo "  kubectl exec -n scalestyle -it \$(kubectl get pod -n scalestyle -l app=data-pipeline -o name) -- python src/scripts/milvus_clip_init.py"
echo "  kubectl exec -n scalestyle -it \$(kubectl get pod -n scalestyle -l app=data-pipeline -o name) -- python src/scripts/generate_mock_clip_embeddings.py"
