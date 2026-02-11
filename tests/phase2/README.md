# Phase 2 Testing Documentation

## 📚 Test Evidence

All test results are saved in the `test-results/` directory:

- `preflight_results_*.md` - A1/A2/A3 gating tests
- `functional_results_*.md` - B1/B2 E2E functional tests
- `degradation_results_*.md` - B3 graceful degradation
- `data_integrity_results_*.md` - C1/C2 Redis + Milvus integrity
- `performance_results_*.md` - D1/E1 performance & concurrency (v2.3 SLO)
- `observability_results_*.md` - G1/G2 traces & metrics

**Latest Test Run** (2026-02-11): All tests (A-G) PASS ✅  
**Key Achievement**: E1 v2.3 - median(p99_all) = 263.26ms ≤ 300ms, degraded_ratio = 0.29%

## 🚀 Quick Start

```bash
# Prerequisites
kubectl port-forward -n scalestyle svc/gateway 8080:8080

# Run all tests (A-G complete suite)
bash run_all_tests.sh

# Run individual test categories
bash preflight_check.sh          # A. Gating tests (MUST PASS first)
bash functional_test.sh          # B1/B2. E2E functional tests
bash degradation_test.sh         # B3. Graceful degradation
bash data_integrity_test.sh      # C. Redis + Milvus integrity
python3 performance_test.py --tests D1,E1  # D/E. Performance & concurrency
bash observability_test.sh       # G. Traces & metrics
```

## 📊 Test Categories Overview

### A. Preflight / Gating Tests ✅
**Script**: `preflight_check.sh`

- **A1**: ray_ratio ≥ 0.95 (steady-state Ray hit rate)
- **A2**: unknown/null items ≤ 1% (result completeness)
- **A3**: Redis, Milvus, Ray operational (dependency health)

### B. E2E Functional Tests ✅
**Scripts**: `functional_test.sh`, `degradation_test.sh`

- **B1**: 11 use cases (basic queries, edge cases, special chars, k limits, Chinese, emoji)
- **B2**: Reason toggle consistency (20/20 checks)
- **B3**: Graceful degradation (degraded_ratio ≥ 80%, error_rate < 5%)

### C. Data Integrity Tests ✅
**Script**: `data_integrity_test.sh`

- **C1**: Redis data (DBSIZE ≥ 100k, popular items, metadata)
- **C2**: Milvus consistency (20/20 searches return results)

### D/E. Performance Tests ✅
**Script**: `performance_test.py`

- **D1**: Baseline (ray_ratio ≥ 95%, p99 < 200ms)
- **E1**: **v2.3 Concurrency Load** (c=10, 3×180s runs):
  - **Gating**: median(p99_all) ≤ 300ms, degraded_ratio ≤ 1%
  - **Diagnostic**: p99_ray_only ≤ 270ms (non-gating)

> **Key Innovation**: v2.3 uses `p99_all` (end-to-end user experience) instead of `p99_ray_only` (manipulable by timeout strategy). p99_all cannot be gamed by internal routing decisions.

### G. Observability Tests ✅
**Script**: `observability_test.sh`

- **G1**: Gateway metrics (`/actuator/metrics`)
- **G2**: Trace coverage (Jaeger integration)

## 🔧 Resource Configuration

### Ray Serve Requirements

Based on deployment configuration:

| Deployment | num_cpus | Purpose |
|------------|----------|---------|
| ingress | 0.25 | API endpoint, routing |
| router | 0.10 | Query routing logic |
| embedding | 2.00 | BGE-large model (CPU) |
| retrieval | 0.50 | Milvus search |
| popularity | 0.10 | Redis fallback |
| reranker | 0.50 | MiniLM cross-encoder |
| generation | 0.10 | Template/LLM generation |
| **Total** | **3.55** | **+ 1-2 CPU overhead** |

**Capacity Formula**:
```
Required Pod CPU ≥ 3.55 + 1.5 (Ray overhead) = 5 CPU minimum
```

### Environment Configurations

**Minikube/Docker Desktop** (`overlays/minikube/resource-limits-small.yaml`):
```yaml
resources:
  requests:
    cpu: "4"      # Works with burst capability
    memory: "8Gi"
  limits:
    cpu: "8"      # Burst capacity
    memory: "12Gi"
```

**EKS Production** (`overlays/eks/resource-limits-production.yaml`):
```yaml
resources:
  requests:
    cpu: "6"      # 3.55 + 1.5 + buffer
    memory: "10Gi"
  limits:
    cpu: "12"     # 2x requests
    memory: "16Gi"
replicas: 2       # High availability
```

## 📁 Test Results & Evidence

All test evidence saved in `test-results/` (local only, not committed to git):

```
test-results/
├── preflight_results_20260211_200709.md       # A1/A2/A3 PASS
├── functional_results_20260211_200820.md      # B1/B2 PASS
├── data_integrity_results_20260211_200830.md  # C1/C2 PASS
├── performance_results_20260211_200843.md     # D1/E1 v2.3 PASS
├── degradation_results_20260211_202232.md     # B3 PASS
└── observability_results_20260211_202641.md   # G1/G2 PASS
```

> **Note**: Test result files are generated locally and excluded from git (see `.gitignore`). Each test run creates timestamped result files for reproducibility tracking.

**Latest Test Achievement** (2026-02-11):
- median(p99_all) = 263.26ms ≤ 300ms (36.74ms operational margin)
- degraded_ratio = 0.29% ≤ 1% (71% under budget)
- Statistical confidence: 3-run median methodology

## 🔍 Troubleshooting

### Common Issues

**Ray Serve pods stuck in "0/1 Ready"**
- **Cause**: Insufficient CPU (num_cpus × deployments > pod CPU)
- **Fix**: Increase `cpu` requests to 5-6, or reduce deployment num_cpus

**Port 8080 connection refused**
- **Cause**: Gateway not port-forwarded or pods not ready
- **Fix**: `kubectl port-forward -n scalestyle svc/gateway 8080:8080`

**High degradation ratio in performance tests**
- **Cause**: Ray Serve replicas scaling down or pods restarting
- **Fix**: Verify inference pods are 1/1 Ready
  ```bash
  kubectl get pods -n scalestyle -l component=inference
  kubectl rollout status deployment/inference -n scalestyle
  ```

**E1 test fails with p99_all > 300ms**
- **Possible causes**:
  1. Lock bottleneck (check if retrieval.py uses Semaphore(8), not Lock)
  2. Timeout too short (should be 300ms in gateway)
  3. Reranker concurrency too high (should be 6, not 15)
- **Fix**: Check v2.3 optimizations (Lock→Semaphore, timeout=300ms, reranker=6)

## 🛠️ Dependencies

- **kubectl**: Kubernetes CLI
- **jq**: JSON processing (`brew install jq`)
- **Python 3.8+**: For performance_test.py
  - `pip install requests numpy`
- **curl**: HTTP testing
- **bash 4.0+**: Shell script execution

---

**Version**: v2.3 (2026-02-11)  
**Status**: All tests (A-G) PASS ✅  
**v2.3 Standards**: p99_all ≤ 300ms (end-to-end), degraded_ratio ≤ 1%, 3-run median methodology
