# ScaleStyle Kubernetes Deployment Makefile
# Week 4: Production K8s deployment

.PHONY: help k8s-setup k8s-apply k8s-delete k8s-status k8s-logs-gateway k8s-logs-inference \
        milvus-install milvus-uninstall data-init build-images push-images deploy-all clean-all

# Default Docker Hub username (override with: make deploy-all DOCKERHUB_USER=yourname)
DOCKERHUB_USER ?= your-dockerhub-username
IMAGE_TAG ?= latest
NAMESPACE := scalestyle

# Colors for output
GREEN := \033[0;32m
YELLOW := \033[0;33m
RED := \033[0;31m
NC := \033[0m # No Color

help: ## Show this help message
	@echo "$(GREEN)ScaleStyle K8s Deployment Commands$(NC)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "$(YELLOW)%-25s$(NC) %s\n", $$1, $$2}'

##@ Setup & Prerequisites

k8s-setup: ## Setup K8s prerequisites (metrics-server, ingress-nginx)
	@echo "$(GREEN)Installing K8s prerequisites...$(NC)"
	@# Enable metrics-server for HPA
	@if command -v minikube >/dev/null 2>&1; then \
		echo "$(YELLOW)Detected Minikube - enabling addons$(NC)"; \
		minikube addons enable metrics-server; \
		minikube addons enable ingress; \
	else \
		echo "$(YELLOW)Installing metrics-server...$(NC)"; \
		kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml; \
		echo "$(YELLOW)Installing ingress-nginx...$(NC)"; \
		kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.8.2/deploy/static/provider/cloud/deploy.yaml; \
	fi
	@echo "$(GREEN)✓ Prerequisites installed$(NC)"

##@ Core Deployment

deploy-minikube: ## Deploy to Minikube (local development)
	@echo "$(GREEN)Deploying to Minikube...$(NC)"
	@./infrastructure/k8s/deploy.sh minikube deploy

deploy-eks: ## Deploy to EKS (AWS production)
	@echo "$(GREEN)Deploying to EKS...$(NC)"
	@./infrastructure/k8s/deploy.sh eks deploy

k8s-apply: deploy-minikube ## Alias for deploy-minikube (backward compatibility)

k8s-delete-minikube: ## Delete Minikube deployment
	@echo "$(RED)Deleting Minikube deployment...$(NC)"
	@./infrastructure/k8s/deploy.sh minikube delete

k8s-delete-eks: ## Delete EKS deployment
	@echo "$(RED)Deleting EKS deployment...$(NC)"
	@./infrastructure/k8s/deploy.sh eks delete
k8s-delete: k8s-delete-minikube ## Alias for k8s-delete-minikube (backward compatibility)

k8s-status: ## Show status of all pods and services
	@echo "$(GREEN)=== Namespace Status ===$(NC)"
	@kubectl get namespace $(NAMESPACE) 2>/dev/null || echo "$(RED)Namespace not found$(NC)"
	@echo ""
	@echo "$(GREEN)=== Pods ===$(NC)"
	@kubectl get pods -n $(NAMESPACE) -o wide
	@echo ""
	@echo "$(GREEN)=== Services ===$(NC)"
	@kubectl get svc -n $(NAMESPACE)
	@echo ""
	@echo "$(GREEN)=== Ingress ===$(NC)"
	@kubectl get ingress -n $(NAMESPACE)
	@echo ""
	@echo "$(GREEN)=== HPA (if deployed) ===$(NC)"
	@kubectl get hpa -n $(NAMESPACE) 2>/dev/null || echo "No HPA deployed yet"

##@ Milvus (Vector Database)

milvus-install: ## Install Milvus using Helm
	@echo "$(GREEN)Installing Milvus via Helm...$(NC)"
	@helm repo add milvus https://zilliztech.github.io/milvus-helm/
	@helm repo update
	@helm upgrade --install milvus milvus/milvus \
		--namespace $(NAMESPACE) \
		--create-namespace \
		-f infrastructure/k8s/helm-values/milvus-standalone.yaml \
		--wait \
		--timeout 10m
	@echo "$(GREEN)✓ Milvus installed$(NC)"
	@echo "$(YELLOW)Milvus will be available at: milvus.$(NAMESPACE).svc.cluster.local:19530$(NC)"

milvus-uninstall: ## Uninstall Milvus
	@echo "$(RED)Uninstalling Milvus...$(NC)"
	@helm uninstall milvus --namespace $(NAMESPACE) || true
	@echo "$(GREEN)✓ Milvus uninstalled$(NC)"

##@ Data Initialization

data-init: ## Run data initialization job
	@echo "$(GREEN)Starting data initialization job...$(NC)"
	@kubectl apply -f infrastructure/k8s/60-init-job.yaml
	@echo "$(YELLOW)Waiting for job to complete (timeout: 5min)...$(NC)"
	@kubectl wait --for=condition=complete --timeout=300s job/data-init -n $(NAMESPACE) || \
		(echo "$(RED)Job failed or timed out. Check logs with: make k8s-logs-init$(NC)" && exit 1)
	@echo "$(GREEN)✓ Data initialization complete$(NC)"

##@ Autoscaling (HPA)

hpa-deploy: ## Deploy Horizontal Pod Autoscaler for gateway
	@echo "$(GREEN)Deploying HPA for gateway...$(NC)"
	@kubectl apply -f infrastructure/k8s/70-gateway-hpa.yaml
	@echo "$(GREEN)✓ HPA deployed$(NC)"
	@echo "$(YELLOW)Run 'kubectl get hpa -n $(NAMESPACE) -w' to watch autoscaling$(NC)"

##@ Logs & Debugging

k8s-logs-gateway: ## Tail gateway logs
	@echo "$(GREEN)Tailing gateway logs (Ctrl+C to stop)...$(NC)"
	@kubectl logs -f -n $(NAMESPACE) -l component=gateway --tail=100

k8s-logs-inference: ## Tail inference logs
	@echo "$(GREEN)Tailing inference logs (Ctrl+C to stop)...$(NC)"
	@kubectl logs -f -n $(NAMESPACE) -l component=inference --tail=100

k8s-logs-redis: ## Tail Redis logs
	@echo "$(GREEN)Tailing Redis logs (Ctrl+C to stop)...$(NC)"
	@kubectl logs -f -n $(NAMESPACE) -l component=redis --tail=100

k8s-logs-init: ## Show data init job logs
	@echo "$(GREEN)Data init job logs:$(NC)"
	@kubectl logs -n $(NAMESPACE) job/data-init

k8s-describe-gateway: ## Describe gateway deployment
	@kubectl describe deployment gateway -n $(NAMESPACE)

k8s-describe-inference: ## Describe inference deployment
	@kubectl describe deployment inference -n $(NAMESPACE)

##@ Docker Images

build-images: ## Build Docker images locally
	@echo "$(GREEN)Building Docker images...$(NC)"
	@docker build -t $(DOCKERHUB_USER)/scalestyle-gateway:$(IMAGE_TAG) ./gateway-service
	@docker build -t $(DOCKERHUB_USER)/scalestyle-inference:$(IMAGE_TAG) ./inference-service
	@echo "$(GREEN)✓ Images built$(NC)"

push-images: ## Push Docker images to Docker Hub
	@echo "$(GREEN)Pushing images to Docker Hub...$(NC)"
	@docker push $(DOCKERHUB_USER)/scalestyle-gateway:$(IMAGE_TAG)
	@docker push $(DOCKERHUB_USER)/scalestyle-inference:$(IMAGE_TAG)
	@echo "$(GREEN)✓ Images pushed$(NC)"

##@ Complete Workflows

deploy-all: k8s-setup k8s-apply milvus-install data-init hpa-deploy ## Complete deployment (setup + deploy + milvus + data + hpa)
	@echo ""
	@echo "$(GREEN)========================================$(NC)"
	@echo "$(GREEN)✓ Complete deployment finished!$(NC)"
	@echo "$(GREEN)========================================$(NC)"
	@echo ""
	@echo "$(YELLOW)Next steps:$(NC)"
	@echo "  1. Check status: make k8s-status"
	@echo "  2. Get Ingress IP: kubectl get ingress -n $(NAMESPACE)"
	@echo "  3. Test API: curl http://<ingress-ip>/api/recommendation/search?query=dress&k=5"
	@echo "  4. Watch HPA: kubectl get hpa -n $(NAMESPACE) -w"
	@echo ""

clean-all: k8s-delete milvus-uninstall ## Clean everything (delete all resources including milvus)
	@echo "$(RED)Cleaning all resources...$(NC)"
	@kubectl delete namespace $(NAMESPACE) --ignore-not-found=true
	@echo "$(GREEN)✓ Cleanup complete$(NC)"

##@ Testing

test-api: ## Test the API endpoint
	@echo "$(GREEN)Testing ScaleStyle API...$(NC)"
	@INGRESS_IP=$$(kubectl get ingress scalestyle-ingress -n $(NAMESPACE) -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || \
				  kubectl get ingress scalestyle-ingress -n $(NAMESPACE) -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || \
				  minikube ip 2>/dev/null || echo "localhost"); \
	echo "$(YELLOW)Testing: http://$$INGRESS_IP/api/recommendation/search?query=dress&k=5$(NC)"; \
	curl -s "http://$$INGRESS_IP/api/recommendation/search?query=dress&k=5" | jq . || \
	curl -s "http://$$INGRESS_IP/api/recommendation/search?query=dress&k=5"

port-forward-gateway: ## Port-forward gateway to localhost:8080
	@echo "$(GREEN)Port-forwarding gateway to localhost:8080$(NC)"
	@kubectl port-forward -n $(NAMESPACE) svc/gateway 8080:8080

port-forward-inference: ## Port-forward inference to localhost:8000
	@echo "$(GREEN)Port-forwarding inference to localhost:8000$(NC)"
	@kubectl port-forward -n $(NAMESPACE) svc/inference 8000:8000
