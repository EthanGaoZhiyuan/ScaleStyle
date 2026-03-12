output "aws_region" {
  description = "AWS region used for the deployment."
  value       = var.aws_region
}

output "aws_account_id" {
  description = "AWS account ID that owns the created resources."
  value       = data.aws_caller_identity.current.account_id
}

output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS cluster API endpoint."
  value       = module.eks.cluster_endpoint
}

output "oidc_provider_arn" {
  description = "OIDC provider ARN for IRSA-enabled add-ons and workloads."
  value       = module.eks.oidc_provider_arn
}

output "vpc_id" {
  description = "VPC ID for the infrastructure."
  value       = module.vpc.vpc_id
}

output "private_subnet_ids" {
  description = "Private subnet IDs used by the EKS cluster and node groups."
  value       = module.vpc.private_subnets
}

output "public_subnet_ids" {
  description = "Public subnet IDs used for NAT and future internet-facing load balancers."
  value       = module.vpc.public_subnets
}

output "ecr_repository_urls" {
  description = "Repository URLs for application images."
  value = {
    for repository_name, repository in aws_ecr_repository.services : repository_name => repository.repository_url
  }
}

output "gateway_service_repository_url" {
  description = "ECR repository URL for gateway-service."
  value       = aws_ecr_repository.services["gateway-service"].repository_url
}

output "inference_service_repository_url" {
  description = "ECR repository URL for inference-service."
  value       = aws_ecr_repository.services["inference-service"].repository_url
}

output "event_consumer_repository_url" {
  description = "ECR repository URL for event-consumer."
  value       = aws_ecr_repository.services["event-consumer"].repository_url
}

output "data_init_repository_url" {
  description = "ECR repository URL for data-init."
  value       = aws_ecr_repository.services["data-init"].repository_url
}

output "s3_bucket_name" {
  description = "S3 bucket for embeddings, parquet files, and demo assets."
  value       = aws_s3_bucket.artifacts.bucket
}

output "redis_primary_endpoint" {
  description = "Primary ElastiCache Redis endpoint for application pods."
  value       = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "redis_port" {
  description = "ElastiCache Redis port."
  value       = aws_elasticache_replication_group.redis.port
}

output "update_kubeconfig_command" {
  description = "Helper command to update local kubeconfig from Terraform outputs."
  value       = "aws eks update-kubeconfig --region ${var.aws_region} --name ${module.eks.cluster_name}"
}