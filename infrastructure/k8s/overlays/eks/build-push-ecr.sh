#!/usr/bin/env bash

set -euo pipefail

TF_DIR=${TF_DIR:-infrastructure/terraform}
# Default to a short git SHA so every build produces an immutable, auditable tag.
# Do NOT use "latest": it makes rollback and incident forensics non-deterministic.
# Override with IMAGE_TAG=<custom-tag> if you need a named release (e.g. "v1.2.3").
IMAGE_TAG=${IMAGE_TAG:-git-$(git rev-parse --short HEAD)}
PLATFORM=${PLATFORM:-linux/amd64}

gateway_repo=$(terraform -chdir="$TF_DIR" output -raw gateway_service_repository_url)
inference_repo=$(terraform -chdir="$TF_DIR" output -raw inference_service_repository_url)
event_consumer_repo=$(terraform -chdir="$TF_DIR" output -raw event_consumer_repository_url)
data_init_repo=$(terraform -chdir="$TF_DIR" output -raw data_init_repository_url)
aws_region=$(terraform -chdir="$TF_DIR" output -raw aws_region)

registry=${gateway_repo%/gateway-service}

aws ecr get-login-password --region "$aws_region" | docker login --username AWS --password-stdin "$registry"

docker buildx build --platform "$PLATFORM" -t "$gateway_repo:$IMAGE_TAG" --push gateway-service
docker buildx build --platform "$PLATFORM" -t "$inference_repo:$IMAGE_TAG" --push inference-service
docker buildx build --platform "$PLATFORM" -t "$event_consumer_repo:$IMAGE_TAG" --push event-consumer
docker buildx build --platform "$PLATFORM" -t "$data_init_repo:$IMAGE_TAG" --push data-pipeline