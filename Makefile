# ScaleStyle Kubernetes Deployment Makefile
# Production K8s deployment

.PHONY: help k8s-setup k8s-apply k8s-delete k8s-status k8s-logs-gateway k8s-logs-inference \
	generate-embeddings-smoke generate-embeddings-full validate-embeddings bootstrap-local-data \
	generate-image-embeddings-smoke generate-image-embeddings-full \
	validate-image-embeddings validate-image-embeddings-smoke \
	bootstrap-image-collection rebuild-image-collection \
	bootstrap-smoke-image-collection image-pipeline-smoke \
	milvus-install milvus-uninstall data-init build-images push-images deploy-all clean-all \
	tf-init tf-plan tf-apply tf-destroy eks-kubeconfig kafka-install-strimzi kafka-deploy \
	kafka-status kafka-topic-verify kafka-smoke eks-sync-ecr-images bootstrap-kafka \
	apply-cloud-config install-alb-controller deploy-milvus deploy-production destroy-production verify-deployment push-ecr-images \
	smoke-text smoke-image smoke-hybrid smoke-fallback smoke-all

# Default Docker Hub username (override with: make deploy-all DOCKERHUB_USER=yourname)
DOCKERHUB_USER ?= your-dockerhub-username
IMAGE_TAG ?= latest
NAMESPACE := scalestyle
TF_DIR := infrastructure/terraform

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

tf-init: ## Initialize Terraform for AWS foundation
	terraform -chdir=$(TF_DIR) init

tf-plan: ## Plan AWS foundation changes
	terraform -chdir=$(TF_DIR) plan

tf-apply: ## Apply AWS foundation changes
	terraform -chdir=$(TF_DIR) apply

tf-destroy: ## Destroy AWS foundation
	terraform -chdir=$(TF_DIR) destroy

bootstrap-kafka: ## Apply Terraform, update kubeconfig, install Strimzi, deploy and verify Kafka
	@./infrastructure/terraform/bootstrap-kafka.sh

eks-kubeconfig: ## Update kubeconfig from Terraform outputs
	@./infrastructure/terraform/update-kubeconfig.sh

apply-cloud-config: ## Create/update cloud config ConfigMap from Terraform outputs
	@./infrastructure/k8s/overlays/eks/apply-cloud-config.sh

install-alb-controller: ## Install AWS Load Balancer Controller on EKS
	@./infrastructure/k8s/overlays/eks/install-alb-controller.sh

deploy-milvus: ## Deploy Milvus standalone on EKS via Helm
	@./infrastructure/k8s/overlays/eks/deploy-milvus.sh

push-ecr-images: ## Build and push gateway, inference, event-consumer, and data-init images to ECR
	@./infrastructure/k8s/overlays/eks/build-push-ecr.sh

deploy-production: ## Apply production cloud app stack to EKS
	@./infrastructure/k8s/overlays/eks/deploy-production.sh

destroy-production: ## Safely tear down the production EKS stack and Terraform foundation
	@./infrastructure/k8s/overlays/eks/destroy-production.sh --environment production $(DESTROY_ARGS)

verify-deployment: ## Verify app stack, public endpoints, and realtime loop
	@./infrastructure/k8s/overlays/eks/verify-deployment.sh

eks-sync-ecr-images: ## Rewrite EKS overlay image URLs from Terraform outputs
	@./infrastructure/k8s/overlays/eks/sync-ecr-images.sh

kafka-install-strimzi: ## Install Strimzi operator on EKS
	@./infrastructure/k8s/overlays/eks/kafka/install-strimzi.sh

kafka-deploy: ## Deploy Kafka cluster and required topics on EKS
	@./infrastructure/k8s/overlays/eks/kafka/deploy-kafka.sh

kafka-status: ## Verify Strimzi and Kafka health
	@./infrastructure/k8s/overlays/eks/kafka/verify-kafka.sh

kafka-topic-verify: ## Verify scalestyle.clicks topic exists
	@TOPIC_ONLY=1 ./infrastructure/k8s/overlays/eks/kafka/verify-kafka.sh

kafka-smoke: kafka-install-strimzi kafka-deploy kafka-status ## Install, deploy, and verify Kafka on EKS

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

##@ Data Pipeline (Embedding Generation + Bootstrap)

PIPELINE_DIR := data-pipeline
EMBEDDING_OUTPUT := $(PIPELINE_DIR)/data/processed/article_embeddings_bge_small_v1_5_detail.parquet

generate-embeddings-smoke: ## Smoke test: embed first 100 articles (no GPU required)
	@echo "$(GREEN)Running embedding smoke test (100 articles)…$(NC)"
	cd $(PIPELINE_DIR) && python src/generate_embeddings.py --limit 100 --overwrite
	@echo "$(GREEN)✓ Smoke generation complete$(NC)"

generate-embeddings-full: ## Full embedding generation (~105K articles) — runs in data-pipeline/
	@echo "$(YELLOW)Full embedding generation. This may take 15–30 min on CPU.$(NC)"
	cd $(PIPELINE_DIR) && python src/generate_embeddings.py --overwrite

validate-embeddings: ## Validate the active embedding parquet artifact
	@echo "$(GREEN)Validating embedding parquet…$(NC)"
	cd $(PIPELINE_DIR) && python src/validate_embeddings.py \
		--input data/processed/article_embeddings_bge_small_v1_5_detail.parquet \
		--expected-dim 384

bootstrap-local-data: ## Load embedding parquet into local Milvus + Redis (docker-compose must be up)
	@echo "$(GREEN)Bootstrapping local Milvus + Redis…$(NC)"
	cd $(PIPELINE_DIR) && python src/bootstrap_data.py \
		--parquet data/processed/article_embeddings_bge_small_v1_5_detail.parquet \
		--drop-existing
	@echo "$(GREEN)✓ Local bootstrap complete$(NC)"

# Image embedding pipeline variables (openai/clip-vit-base-patch32, 512-d)
# Full artifact — written only by generate-image-embeddings-full, read by validate/bootstrap targets
IMAGE_EMBEDDING_PARQUET       := data/processed/article_image_embeddings_clip_vit_base_patch32.parquet
IMAGE_COLLECTION              := scale_style_clip_image_v1
IMAGE_EMBEDDING_DIM           := 512
# Smoke artifact — written only by generate-image-embeddings-smoke; never overwrites the full artifact
IMAGE_EMBEDDING_SMOKE_PARQUET := data/processed/smoke/article_image_embeddings_clip_vit_base_patch32_smoke.parquet
IMAGE_SMOKE_COLLECTION        := scale_style_clip_image_smoke_v1

generate-image-embeddings-smoke: ## Smoke test: generate image embeddings for 100 articles → data/processed/smoke/ (never touches full artifact)
	@echo "$(GREEN)Running image embedding smoke test (100 articles)…$(NC)"
	@echo "$(YELLOW)  output: $(IMAGE_EMBEDDING_SMOKE_PARQUET)$(NC)"
	cd $(PIPELINE_DIR) && python src/generate_image_embeddings.py \
		--output $(IMAGE_EMBEDDING_SMOKE_PARQUET) \
		--limit 100 \
		--overwrite
	@echo "$(GREEN)✓ Image embedding smoke complete$(NC)"

generate-image-embeddings-full: ## Full image embedding generation (~105K images) — WARNING: 20–60+ min on CPU; writes $(IMAGE_EMBEDDING_PARQUET)
	@echo "$(RED)WARNING: This writes the FULL artifact and may take 20–60+ minutes on CPU.$(NC)"
	@echo "$(RED)         Output: $(IMAGE_EMBEDDING_PARQUET)$(NC)"
	@echo "$(YELLOW)         Press Ctrl+C to cancel.$(NC)"
	cd $(PIPELINE_DIR) && python src/generate_image_embeddings.py \
		--output $(IMAGE_EMBEDDING_PARQUET) \
		--overwrite

validate-image-embeddings: ## Validate the FULL image embedding artifact (expected dim: 512)
	@echo "$(GREEN)Validating full image embedding parquet…$(NC)"
	cd $(PIPELINE_DIR) && python src/validate_image_embeddings.py \
		--input $(IMAGE_EMBEDDING_PARQUET) \
		--expected-dim $(IMAGE_EMBEDDING_DIM)

validate-image-embeddings-smoke: ## Validate the SMOKE image embedding artifact (expected dim: 512)
	@echo "$(GREEN)Validating smoke image embedding parquet…$(NC)"
	cd $(PIPELINE_DIR) && python src/validate_image_embeddings.py \
		--input $(IMAGE_EMBEDDING_SMOKE_PARQUET) \
		--expected-dim $(IMAGE_EMBEDDING_DIM)

bootstrap-image-collection: ## Bootstrap FULL image Milvus collection (safe: fails if collection exists — see rebuild-image-collection)
	@echo "$(GREEN)Bootstrapping Milvus image collection: $(IMAGE_COLLECTION)…$(NC)"
	cd $(PIPELINE_DIR) && python src/bootstrap_image_collection.py \
		--parquet $(IMAGE_EMBEDDING_PARQUET) \
		--collection $(IMAGE_COLLECTION) \
		--expected-dim $(IMAGE_EMBEDDING_DIM)
	@echo "$(GREEN)✓ Image collection bootstrap complete$(NC)"

rebuild-image-collection: ## Drop and rebuild FULL image Milvus collection — WARNING: destroys existing vectors
	@echo "$(RED)WARNING: Dropping and rebuilding collection '$(IMAGE_COLLECTION)'. All existing vectors will be lost.$(NC)"
	cd $(PIPELINE_DIR) && python src/bootstrap_image_collection.py \
		--parquet $(IMAGE_EMBEDDING_PARQUET) \
		--collection $(IMAGE_COLLECTION) \
		--expected-dim $(IMAGE_EMBEDDING_DIM) \
		--drop-existing
	@echo "$(GREEN)✓ Image collection rebuilt$(NC)"

bootstrap-smoke-image-collection: ## Bootstrap SMOKE image Milvus collection from smoke artifact (100 items, separate collection)
	@echo "$(GREEN)Bootstrapping smoke Milvus image collection: $(IMAGE_SMOKE_COLLECTION)…$(NC)"
	cd $(PIPELINE_DIR) && python src/bootstrap_image_collection.py \
		--parquet $(IMAGE_EMBEDDING_SMOKE_PARQUET) \
		--collection $(IMAGE_SMOKE_COLLECTION) \
		--expected-dim $(IMAGE_EMBEDDING_DIM) \
		--drop-existing
	@echo "$(GREEN)✓ Smoke image collection bootstrap complete$(NC)"

image-pipeline-smoke: generate-image-embeddings-smoke validate-image-embeddings-smoke bootstrap-smoke-image-collection ## Smoke pipeline: generate (100 items) → validate → load into smoke collection (never touches full artifact or full collection)

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

##@ Local Smoke Tests (docker-compose)

smoke-text: ## Text search smoke test (GET /api/recommendation/search)
	@bash scripts/smoke_text_search.sh

smoke-image: ## Image search smoke test (POST /api/recommendation/search/image)
	@bash scripts/smoke_image_search.sh

smoke-hybrid: ## Hybrid text+image search smoke test (POST /api/recommendation/search/hybrid)
	@bash scripts/smoke_hybrid_search.sh

smoke-fallback: ## Fallback resilience smoke test (non-destructive by default; DESTRUCTIVE=1 pauses inference)
	@bash scripts/smoke_fallback.sh

smoke-all: ## Run all non-destructive smoke tests
	@bash scripts/smoke_all.sh
