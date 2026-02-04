#!/bin/bash
# ScaleStyle Repository Cleanup Script
# Removes all non-production files and reduces repository size

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}ScaleStyle Repository Cleanup${NC}"
echo -e "${YELLOW}This will remove non-production files to reduce repository size${NC}"
echo ""

# Calculate current size
BEFORE_SIZE=$(du -sh . 2>/dev/null | cut -f1)
echo -e "${GREEN}Current size: $BEFORE_SIZE${NC}"
echo ""

read -p "Continue with cleanup? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cleanup cancelled"
    exit 0
fi

echo -e "${YELLOW}Starting cleanup...${NC}"
echo ""

# ============================================
# Category A: Cache, IDE, Local Environment
# ============================================
echo -e "${GREEN}[1/7] Removing cache and IDE files...${NC}"

# Python cache
find . -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find . -type d -name ".pytest_cache" -prune -exec rm -rf {} + 2>/dev/null || true
find . -type d -name ".ruff_cache" -prune -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true

# IDE configs
rm -rf .idea 2>/dev/null || true
rm -rf .vscode 2>/dev/null || true

# Virtual environments
rm -rf .venv 2>/dev/null || true
rm -rf venv 2>/dev/null || true

# Git (if you want to remove for final delivery)
# rm -rf .git 2>/dev/null || true

echo -e "${GREEN}✓ Cache and IDE files removed${NC}"

# ============================================
# Category B: Documentation (Non-Production)
# ============================================
echo -e "${GREEN}[2/7] Removing non-production documentation...${NC}"

rm -rf docs 2>/dev/null || true
rm -f CODE_REVIEW_*.md 2>/dev/null || true
rm -f WEEK*.md 2>/dev/null || true

echo -e "${GREEN}✓ Documentation removed${NC}"

# ============================================
# Category C: Development Scripts
# ============================================
echo -e "${GREEN}[3/7] Removing development scripts...${NC}"

rm -rf scripts 2>/dev/null || true
rm -f final-acceptance-test.sh 2>/dev/null || true
rm -f verify-traces.sh 2>/dev/null || true
rm -f week3-verify.sh 2>/dev/null || true

echo -e "${GREEN}✓ Development scripts removed${NC}"

# ============================================
# Category D: Load Testing Results
# ============================================
echo -e "${GREEN}[4/7] Removing load test results...${NC}"

rm -rf loadtest/results 2>/dev/null || true
rm -rf loadtest/__pycache__ 2>/dev/null || true

# Optional: Remove entire loadtest directory
# rm -rf loadtest 2>/dev/null || true

echo -e "${GREEN}✓ Load test results removed${NC}"

# ============================================
# Category E: CI/CD (Optional)
# ============================================
echo -e "${GREEN}[5/7] Removing CI/CD configs (optional)...${NC}"

# Uncomment if you don't need GitHub Actions
# rm -rf .github 2>/dev/null || true

# Remove AI assistant configs
rm -rf .ai 2>/dev/null || true

echo -e "${GREEN}✓ CI/CD configs handled${NC}"

# ============================================
# Category F: Legacy K8s (Already Moved)
# ============================================
echo -e "${GREEN}[6/7] Removing legacy K8s configs...${NC}"

rm -rf infrastructure/k8s-legacy 2>/dev/null || true

echo -e "${GREEN}✓ Legacy K8s configs removed${NC}"

# ============================================
# Category G: Large Data Files
# ============================================
echo -e "${GREEN}[7/7] Removing large data files...${NC}"

# Raw data
rm -rf data-pipeline/data/raw 2>/dev/null || true

# Embeddings (large parquet files)
rm -f data-pipeline/data/article_embeddings_bge_detail.parquet 2>/dev/null || true
rm -f data-pipeline/data/article_embeddings_bge_v2.parquet 2>/dev/null || true
rm -f data-pipeline/data/*.parquet 2>/dev/null || true

# Processed metadata (regenerate with init-job)
rm -f data-pipeline/data/processed/product_metadata.json 2>/dev/null || true

# Top items (regenerate with init-job)
rm -rf data-pipeline/data/processed/top_items_parquet 2>/dev/null || true

echo -e "${GREEN}✓ Large data files removed${NC}"

# ============================================
# Category H: Duplicate Grafana Configs
# ============================================
echo -e "${GREEN}[Bonus] Removing duplicate Grafana configs...${NC}"

rm -f observability/grafana/provisioning/dashboards/dashboard.yml 2>/dev/null || true
rm -f observability/grafana/provisioning/datasources/datasource.yml 2>/dev/null || true

echo -e "${GREEN}✓ Duplicate configs removed${NC}"

# ============================================
# Category I: Java Build Artifacts (Optional)
# ============================================
echo -e "${GREEN}[Bonus] Removing Java build artifacts...${NC}"

rm -rf gateway-service/build 2>/dev/null || true
rm -rf gateway-service/bin 2>/dev/null || true
rm -rf gateway-service/.gradle 2>/dev/null || true

echo -e "${GREEN}✓ Java build artifacts removed${NC}"

# ============================================
# Summary
# ============================================
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Cleanup Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

AFTER_SIZE=$(du -sh . 2>/dev/null | cut -f1)
echo -e "${GREEN}Before: $BEFORE_SIZE${NC}"
echo -e "${GREEN}After:  $AFTER_SIZE${NC}"
echo ""

echo -e "${YELLOW}Removed:${NC}"
echo "  ✓ Python cache (__pycache__, .pytest_cache, .ruff_cache)"
echo "  ✓ IDE configs (.idea, .vscode)"
echo "  ✓ Virtual environments (.venv)"
echo "  ✓ Documentation (docs/, CODE_REVIEW_*.md)"
echo "  ✓ Development scripts (scripts/)"
echo "  ✓ Load test results (loadtest/results/)"
echo "  ✓ AI configs (.ai/)"
echo "  ✓ Legacy K8s (infrastructure/k8s-legacy/)"
echo "  ✓ Large data files (*.parquet, raw CSV, processed JSON)"
echo "  ✓ Duplicate Grafana configs"
echo "  ✓ Java build artifacts"
echo ""

echo -e "${YELLOW}Kept (Production Essentials):${NC}"
echo "  ✓ Source code (gateway-service/, inference-service/, data-pipeline/)"
echo "  ✓ K8s manifests (infrastructure/k8s/)"
echo "  ✓ Chaos tests (tests/chaos/)"
echo "  ✓ Docker configs (Dockerfile, docker-compose.yml)"
echo "  ✓ Configuration files (requirements.txt, build.gradle, etc.)"
echo "  ✓ Observability (observability/)"
echo "  ✓ Makefile"
echo "  ✓ README.md"
echo ""

echo -e "${GREEN}Next steps:${NC}"
echo "  1. Test deployment: make deploy-minikube"
echo "  2. Verify services work"
echo "  3. Commit changes: git add -A && git commit -m 'chore: cleanup non-production files'"
echo ""
