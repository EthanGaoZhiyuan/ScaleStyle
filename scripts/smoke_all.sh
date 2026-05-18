#!/usr/bin/env bash
# Run all non-destructive smoke tests in sequence.
# Exits on first failure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "========================================"
echo "  ScaleStyle Local Smoke Suite"
echo "========================================"
echo ""

bash "${SCRIPT_DIR}/smoke_text_search.sh"
echo ""
bash "${SCRIPT_DIR}/smoke_image_search.sh"
echo ""
bash "${SCRIPT_DIR}/smoke_hybrid_search.sh"
echo ""
bash "${SCRIPT_DIR}/smoke_fallback.sh"
echo ""

echo "========================================"
echo "  ALL SMOKE TESTS PASSED"
echo "========================================"
