# ScaleStyle Kustomize Deployment Guide

## 🎯 Architecture: Local → Cloud Migration

This directory contains **Kustomize-based** Kubernetes configurations that allow seamless deployment across:
- **Minikube** (local development)
- **AWS EKS** (production cloud)

## 📂 Directory Structure

```
k8s-kustomize/
├── base/                     # Environment-agnostic resources
│   ├── namespace.yaml
│   ├── configmap.yaml
│   ├── redis.yaml
│   ├── inference.yaml
│   ├── gateway.yaml
│   ├── jaeger.yaml
│   ├── init-job.yaml
│   ├── gateway-hpa.yaml
│   └── kustomization.yaml
│
├── overlays/
│   ├── minikube/            # Local development
│   │   ├── kustomization.yaml
│   │   ├── ingress-minikube.yaml
│   │   └── resource-limits-small.yaml
│   │
│   └── eks/                 # AWS EKS production
│       ├── kustomization.yaml
│       ├── ingress-alb.yaml
│       ├── storage-class-ebs.yaml
│       ├── service-account-irsa.yaml
│       ├── redis-pvc.yaml
│       ├── milvus-pvc.yaml
│       ├── resource-limits-production.yaml
│       └── replicas-production.yaml
│
└── deploy.sh                # Unified deployment script
```

## 🚀 Quick Start

### Option 1: Using Deploy Script (Recommended)

```bash
# Deploy to Minikube
./infrastructure/k8s-kustomize/deploy.sh minikube deploy

# Deploy to EKS
./infrastructure/k8s-kustomize/deploy.sh eks deploy

# Preview changes before deploying
./infrastructure/k8s-kustomize/deploy.sh eks diff

# Delete deployment
./infrastructure/k8s-kustomize/deploy.sh minikube delete
```

### Option 2: Using Kustomize Directly

```bash
# Minikube
kustomize build infrastructure/k8s-kustomize/overlays/minikube | kubectl apply -f -

# EKS
kustomize build infrastructure/k8s-kustomize/overlays/eks | kubectl apply -f -
```

### Option 3: Using kubectl (No kustomize binary needed)

```bash
# Minikube
kubectl apply -k infrastructure/k8s-kustomize/overlays/minikube

# EKS
kubectl apply -k infrastructure/k8s-kustomize/overlays/eks
```

---

## 🏠 Minikube Deployment

### Prerequisites

```bash
# Install Minikube
brew install minikube

# Start Minikube
minikube start --cpus=4 --memory=8192

# Enable addons
minikube addons enable metrics-server
minikube addons enable ingress
```

### Deploy

```bash
# Deploy all resources
./infrastructure/k8s-kustomize/deploy.sh minikube deploy

# Wait for pods
kubectl wait --for=condition=Ready pod --all -n scalestyle --timeout=300s

# Check status
kubectl get all -n scalestyle
```

### Access Services

```bash
# Port forward gateway
kubectl port-forward -n scalestyle svc/local-gateway 8080:8080

# Port forward Jaeger
kubectl port-forward -n scalestyle svc/local-jaeger-query 16686:16686

# Test API
curl http://localhost:8080/api/recommendation/search?query=dress&k=5 | jq

# View Jaeger UI
open http://localhost:16686
```

### Minikube Configuration

- **Resource limits**: Small (optimized for laptop)
  - Gateway: 100m CPU, 256Mi RAM
  - Inference: 500m CPU, 1Gi RAM
- **Replicas**: 1-2 per service
- **Storage**: hostPath (ephemeral)
- **Ingress**: nginx (built-in)
- **Service type**: NodePort

---

## ☁️ EKS Deployment

### Prerequisites

#### 1. Install AWS CLI & eksctl

```bash
brew install awscli eksctl

# Configure AWS credentials
aws configure
```

#### 2. Create EKS Cluster

```bash
# Option A: Using eksctl (Recommended)
eksctl create cluster \
  --name scalestyle-prod \
  --region ap-southeast-2 \
  --node-type t3.medium \
  --nodes 3 \
  --nodes-min 2 \
  --nodes-max 5 \
  --managed

# Option B: Using Terraform (see infrastructure/terraform/)
```

#### 3. Install Required Add-ons

##### AWS Load Balancer Controller (for ALB Ingress)

```bash
# Create IAM policy
curl -o iam_policy.json https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/main/docs/install/iam_policy.json

aws iam create-policy \
    --policy-name AWSLoadBalancerControllerIAMPolicy \
    --policy-document file://iam_policy.json

# Install controller
eksctl create iamserviceaccount \
  --cluster=scalestyle-prod \
  --namespace=kube-system \
  --name=aws-load-balancer-controller \
  --attach-policy-arn=arn:aws:iam::<AWS_ACCOUNT_ID>:policy/AWSLoadBalancerControllerIAMPolicy \
  --approve

helm repo add eks https://aws.github.io/eks-charts
helm repo update

helm install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system \
  --set clusterName=scalestyle-prod \
  --set serviceAccount.create=false \
  --set serviceAccount.name=aws-load-balancer-controller
```

##### EBS CSI Driver (for PersistentVolumes)

```bash
eksctl create iamserviceaccount \
  --name ebs-csi-controller-sa \
  --namespace kube-system \
  --cluster scalestyle-prod \
  --attach-policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy \
  --approve \
  --role-only \
  --role-name AmazonEKS_EBS_CSI_DriverRole

eksctl create addon \
  --name aws-ebs-csi-driver \
  --cluster scalestyle-prod \
  --service-account-role-arn arn:aws:iam::<AWS_ACCOUNT_ID>:role/AmazonEKS_EBS_CSI_DriverRole \
  --force
```

##### Metrics Server (for HPA)

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
```

#### 4. Configure ECR (Optional - or use Docker Hub)

```bash
# Create ECR repositories
aws ecr create-repository --repository-name scalestyle-gateway --region ap-southeast-2
aws ecr create-repository --repository-name scalestyle-inference --region ap-southeast-2
aws ecr create-repository --repository-name scalestyle-data-init --region ap-southeast-2

# Login to ECR
aws ecr get-login-password --region ap-southeast-2 | docker login --username AWS --password-stdin <AWS_ACCOUNT_ID>.dkr.ecr.ap-southeast-2.amazonaws.com

# Tag and push images
docker tag scalestyle-gateway:latest <AWS_ACCOUNT_ID>.dkr.ecr.ap-southeast-2.amazonaws.com/scalestyle-gateway:latest
docker push <AWS_ACCOUNT_ID>.dkr.ecr.ap-southeast-2.amazonaws.com/scalestyle-gateway:latest

# Update kustomization.yaml with ECR URLs
```

### Deploy to EKS

```bash
# Verify cluster
kubectl config current-context
kubectl get nodes

# Preview changes
./infrastructure/k8s-kustomize/deploy.sh eks diff

# Deploy
./infrastructure/k8s-kustomize/deploy.sh eks deploy

# Wait for pods
kubectl wait --for=condition=Ready pod --all -n scalestyle --timeout=600s

# Check status
kubectl get all -n scalestyle
kubectl get ingress -n scalestyle
```

### Access Services

```bash
# Get ALB URL
kubectl get ingress -n scalestyle -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}'

# Test API (replace with your ALB URL)
curl http://api.scalestyle.example.com/api/recommendation/search?query=dress&k=5 | jq

# Access Jaeger
open http://jaeger.scalestyle.example.com

# Access Grafana
open http://grafana.scalestyle.example.com
```

### EKS Configuration

- **Resource limits**: Production-sized
  - Gateway: 500m-2 CPU, 1-2Gi RAM
  - Inference: 2-4 CPU, 4-8Gi RAM
- **Replicas**: 3 gateway, 2 inference (HA)
- **Storage**: EBS gp3 volumes (persistent)
- **Ingress**: ALB (internet-facing)
- **Service type**: LoadBalancer
- **IAM**: IRSA for secure AWS service access

---

## 🔄 Migration Path

### Phase 1: Local Development (Minikube)

1. ✅ Deploy to Minikube
2. ✅ Validate all pods Running
3. ✅ Run init-job (data loaded)
4. ✅ Test API endpoints
5. ✅ Verify HPA scaling
6. ✅ Execute chaos tests
7. ✅ Capture screenshots

### Phase 2: Cloud Migration (EKS)

1. ✅ Create EKS cluster
2. ✅ Install AWS controllers (ALB, EBS CSI)
3. ✅ Push images to ECR
4. ✅ Update kustomization.yaml (image URLs)
5. ✅ Deploy to EKS
6. ✅ Configure DNS (Route53)
7. ✅ Verify production behavior
8. ✅ Run chaos tests on EKS
9. ✅ Document cost

---

## 📊 Key Differences: Minikube vs EKS

| Feature | Minikube | EKS |
|---------|----------|-----|
| **Ingress** | nginx (addon) | AWS ALB |
| **Storage** | hostPath | EBS CSI (gp3) |
| **LoadBalancer** | NodePort | ELB/NLB |
| **Scaling** | Manual/HPA | HPA + Cluster Autoscaler |
| **Monitoring** | In-cluster | CloudWatch integration |
| **DNS** | /etc/hosts | Route53 |
| **Cost** | Free | $2-5/day |
| **Public access** | No | Yes |

---

## 🎯 Deliverables for Portfolio

### Screenshots to Capture

1. **Minikube**:
   - `kubectl get all -n scalestyle`
   - API response with `source=ray`
   - Jaeger trace waterfall
   - Grafana dashboard

2. **EKS**:
   - AWS Console (EKS cluster)
   - `kubectl get all -n scalestyle`
   - ALB in AWS Console
   - Public API response
   - CloudWatch metrics

3. **Chaos Testing**:
   - Before: Normal operation
   - During: Pod deletion + degraded response
   - After: Auto-recovery

### Documentation to Write

1. **Architecture diagram**: Compose → K8s → EKS
2. **Cost breakdown**: EKS node costs, ALB costs
3. **Deployment guide**: This file
4. **Chaos report**: RTO, error rates, P99 latency

---

## 💰 Cost Management

### EKS Estimated Costs (ap-southeast-2)

- **EKS control plane**: $0.10/hour = $73/month
- **EC2 nodes** (3x t3.medium): $0.0416/hour × 3 = $90/month
- **ALB**: $0.0225/hour = $16/month
- **EBS volumes**: $0.08/GB/month × 70GB = $6/month

**Total**: ~$185/month or **$6/day**

### Cost Optimization

```bash
# Stop cluster when not in use
eksctl scale nodegroup --cluster=scalestyle-prod --nodes=0 --name=<nodegroup-name>

# Delete cluster completely
eksctl delete cluster --name=scalestyle-prod --region=ap-southeast-2

# Use spot instances (save 70%)
eksctl create nodegroup \
  --cluster=scalestyle-prod \
  --spot \
  --instance-types=t3.medium,t3a.medium
```

---

## 🐛 Troubleshooting

### Minikube

```bash
# Pods stuck in Pending
kubectl describe pod -n scalestyle <pod-name>
# Check: insufficient CPU/memory

# ImagePullBackOff
minikube ssh docker images
# Ensure images are available

# HPA not scaling
kubectl get hpa -n scalestyle
kubectl top pods -n scalestyle
# Enable metrics-server addon
```

### EKS

```bash
# ALB not created
kubectl logs -n kube-system -l app.kubernetes.io/name=aws-load-balancer-controller

# PVC pending
kubectl describe pvc -n scalestyle
# Verify EBS CSI driver installed

# Pods can't pull from ECR
# Check IRSA/IAM roles
kubectl describe pod -n scalestyle <pod-name>
```

---

## 📚 Next Steps

1. **Minikube**: Follow [QUICKSTART.md](../k8s/QUICKSTART.md)
2. **Chaos Tests**: Execute [chaos_test.sh](../k8s/chaos/chaos_test.sh)
3. **EKS Setup**: Follow this guide's EKS section
4. **Monitoring**: Add Prometheus + Grafana dashboards
5. **CI/CD**: Add GitHub Actions for auto-deploy

---

## 🎓 Learning Resources

- [Kustomize Documentation](https://kustomize.io/)
- [EKS Workshop](https://www.eksworkshop.com/)
- [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/)
- [EBS CSI Driver](https://github.com/kubernetes-sigs/aws-ebs-csi-driver)
