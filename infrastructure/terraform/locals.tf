locals {
  name_prefix = "${var.project_name}-${var.environment}"

  azs = slice(data.aws_availability_zones.available.names, 0, var.availability_zone_count)

  public_subnet_cidrs = [
    for index in range(var.availability_zone_count) : cidrsubnet(var.vpc_cidr, 4, index)
  ]

  private_subnet_cidrs = [
    for index in range(var.availability_zone_count) : cidrsubnet(var.vpc_cidr, 4, index + var.availability_zone_count)
  ]

  ecr_repositories = toset([
    "gateway-service",
    "inference-service",
    "event-consumer",
    "data-init",
  ])

  common_tags = merge(
    {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
      Phase       = "production"
    },
    var.additional_tags,
  )
}

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

locals {
  artifacts_bucket_name = coalesce(
    var.artifacts_bucket_name,
    "${var.project_name}-${var.environment}-assets-${random_id.bucket_suffix.hex}",
  )
}