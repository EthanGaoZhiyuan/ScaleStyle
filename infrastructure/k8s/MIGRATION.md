# Migration Guide: Unified Kubernetes Configuration

## 🎯 What Changed?

**Before**: Two separate K8s configurations
- `infrastructure/k8s/` - Raw YAML files (00-80 series)
- `infrastructure/k8s-kustomize/` - Kustomize overlays

**After**: Single unified configuration
- `infrastructure/k8s/` - Kustomize-based (base + overlays)
- `tests/chaos/` - Chaos testing scripts
- `infrastructure/k8s-legacy/` - Old files (temporary, will be deleted)

## 📦 New Structure

```
infrastructure/k8s/
├── base/                       # Environment-agnostic resources
│   ├── namespace.yaml
│   ├── configmap.yaml
│   ├── redis.yaml
│   ├── inference.yaml
│   ├── gateway.yaml
│   ├── ingress.yaml
│   ├── jaeger.yaml
│   ├── init-job.yaml
│   ├── gateway-hpa.yaml
│   └── kustomization.yaml
│
├── overlays/
│   ├── minikube/              # Local development
│   │   ├── kustomization.yaml
│   │   ├── ingress-minikube.yaml
│   │   ├── resource-limits-small.yaml
│   │   ├── init-job-fast.yaml
│   │   └── inference-hpa.yaml
│   │
│   └── eks/                   # AWS production
│       ├── kustomization.yaml
│       ├── ingress-alb.yaml
│       ├── storage-class-ebs.yaml
│       ├── service-account-irsa.yaml
│       ├── redis-pvc.yaml
│       ├── milvus-pvc.yaml
│       ├── raycluster.yaml
│       ├── resource-limits-production.yaml
│       ├── replicas-production.yaml
│       └── init-job-full.yaml
│
├── helm-values/               # Helm chart values
│   ├── milvus-standalone.yaml
│   └── kuberay-operator.yaml
│
├── deploy.sh                  # Unified deployment script
└── README.md                  # Main documentation

tests/chaos/                   # Testing assets
├── chaos_test.sh
├── locustfile.py
└── RESULTS.md
```

## 🔄 Migration Mapping

| Old Location | New Location | Notes |
|--------------|--------------|-------|
| `k8s/00-namespace.yaml` | `k8s/base/namespace.yaml` | ✅ Migrated |
| `k8s/10-configmap.yaml` | `k8s/base/configmap.yaml` | ✅ Migrated |
| `k8s/20-redis.yaml` | `k8s/base/redis.yaml` | ✅ Migrated |
| `k8s/30-inference.yaml` | `k8s/base/inference.yaml` | ✅ Migrated |
| `k8s/40-gateway.yaml` | `k8s/base/gateway.yaml` | ✅ Migrated |
| `k8s/50-ingress.yaml` | `k8s/base/ingress.yaml` | ✅ Migrated |
| `k8s/55-jaeger.yaml` | `k8s/base/jaeger.yaml` | ✅ Migrated |
| `k8s/60-init-job.yaml` | `k8s/base/init-job.yaml` | ✅ Migrated |
| `k8s/60-init-job.full.yaml` | `k8s/overlays/eks/init-job-full.yaml` | ✅ Replaced with patch |
| `k8s/70-gateway-hpa.yaml` | `k8s/base/gateway-hpa.yaml` | ✅ Migrated |
| `k8s/70-inference-hpa.optional.yaml` | `k8s/overlays/minikube/inference-hpa.yaml` | ✅ Migrated |
| `k8s/80-raycluster.yaml` | `k8s/overlays/eks/raycluster.yaml` | ✅ Migrated |
| `k8s/chaos/` | `tests/chaos/` | ✅ Moved to tests |
| `k8s/helm-values/` | `k8s/helm-values/` | ✅ Kept in place |
| `k8s/QUICKSTART.md` | ❌ Deleted | Merged into main README |
| `k8s/CLUSTER_SETUP.md` | ❌ Deleted | Merged into main README |

## 📝 Command Changes

### Before (Multiple Ways)

```bash
# Old way 1
kubectl apply -f infrastructure/k8s/00-namespace.yaml
kubectl apply -f infrastructure/k8s/10-configmap.yaml
# ... (many files)

# Old way 2
kubectl apply -k infrastructure/k8s-kustomize/overlays/minikube
```

### After (Single Way)

```bash
# Minikube
./infrastructure/k8s/deploy.sh minikube deploy
# or
kubectl apply -k infrastructure/k8s/overlays/minikube

# EKS
./infrastructure/k8s/deploy.sh eks deploy
# or
kubectl apply -k infrastructure/k8s/overlays/eks
```

## 🎯 Benefits

1. **Single Source of Truth**: No more "which file should I edit?"
2. **Environment Isolation**: Minikube vs EKS differences are explicit
3. **DRY Principle**: Base resources shared, patches only where needed
4. **Clear Testing Separation**: Chaos tests in `tests/` (not mixed with deploy)
5. **Easy Cleanup**: Old `k8s-legacy/` can be deleted once verified

## ✅ Verification Steps

### 1. Test Minikube Deployment

```bash
# Start Minikube
minikube start --cpus=4 --memory=8192
minikube addons enable metrics-server

# Deploy
./infrastructure/k8s/deploy.sh minikube deploy

# Verify
kubectl get all -n scalestyle
kubectl port-forward -n scalestyle svc/local-gateway 8080:8080
curl http://localhost:8080/api/recommendation/search?query=dress&k=5
```

### 2. Test Chaos Scripts

```bash
# Scripts now in tests/chaos
./tests/chaos/chaos_test.sh inference

# Locust
locust -f tests/chaos/locustfile.py --host=http://localhost:8080
```

### 3. Preview EKS Deployment (without applying)

```bash
./infrastructure/k8s/deploy.sh eks diff
```

## 🗑️ Cleanup

Once verified (recommended: 1-2 days), delete legacy:

```bash
rm -rf infrastructure/k8s-legacy/
git add -A
git commit -m "chore: remove legacy k8s configs after migration"
```

## 🆘 Rollback (if needed)

If something breaks:

```bash
# Restore old structure
mv infrastructure/k8s infrastructure/k8s-kustomize
mv infrastructure/k8s-legacy infrastructure/k8s
mv tests/chaos/* infrastructure/k8s/chaos/

# Deploy old way
kubectl apply -f infrastructure/k8s/
```

## 📚 Additional Resources

- [Kustomize Documentation](https://kustomize.io/)
- [Main README](./README.md) - Deployment guide
- [Chaos Testing](../../tests/chaos/) - Testing documentation

---

**Date**: 2026-02-03  
**Author**: Copilot Agent  
**Status**: ✅ Migration Complete
