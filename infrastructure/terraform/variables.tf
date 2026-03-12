variable "aws_region" {
  description = "AWS region for infrastructure."
  type        = string
  default     = "ap-southeast-2"
}

variable "project_name" {
  description = "Project name used in AWS resource naming."
  type        = string
  default     = "scalestyle"
}

variable "environment" {
  description = "Environment name used in resource naming and tags."
  type        = string
  default     = "production"
}

variable "cluster_name" {
  description = "EKS cluster name."
  type        = string
  default     = "scalestyle-eks"
}

variable "cluster_version" {
  description = "Kubernetes version for EKS."
  type        = string
  default     = "1.30"
}

variable "vpc_cidr" {
  description = "VPC CIDR block."
  type        = string
  default     = "10.30.0.0/16"
}

variable "availability_zone_count" {
  description = "Number of availability zones to use."
  type        = number
  default     = 2
}

variable "node_instance_types" {
  description = "Managed node group instance types."
  type        = list(string)
  default     = ["t3.large"]
}

variable "node_min_size" {
  description = "Minimum managed node group size."
  type        = number
  default     = 1
}

variable "node_desired_size" {
  description = "Desired managed node group size."
  type        = number
  default     = 2
}

variable "node_max_size" {
  description = "Maximum managed node group size."
  type        = number
  default     = 3
}

variable "node_disk_size" {
  description = "Disk size in GiB for managed nodes."
  type        = number
  default     = 40
}

variable "redis_node_type" {
  description = "ElastiCache node type for cloud Redis."
  type        = string
  default     = "cache.t4g.small"
}

variable "redis_apply_immediately" {
  description = <<-EOT
    Whether ElastiCache changes should apply immediately.

    Safe production default is false so stateful changes wait for the maintenance
    window instead of causing surprise restarts/failovers during the day.
    Non-production environments can override this to true when fast iteration is
    worth the risk.
  EOT
  type    = bool
  default = false
}

variable "redis_preferred_maintenance_window" {
  description = <<-EOT
    Weekly ElastiCache maintenance window in UTC (ddd:hh24:mi-ddd:hh24:mi).

    Keep this explicit so engine patching, parameter changes, and any deferred
    stateful modifications happen in an operator-chosen window.
  EOT
  type    = string
  default = "sun:16:00-sun:18:00"
}

variable "redis_snapshot_window" {
  description = <<-EOT
    Daily Redis snapshot window in UTC (hh24:mi-hh24:mi).

    Snapshot timing should be predictable and separated from the maintenance
    window where possible.
  EOT
  type    = string
  default = "18:00-19:00"
}

variable "redis_snapshot_retention_limit" {
  description = "Number of daily Redis snapshots to retain for point-in-time recovery."
  type        = number
  default     = 7
}

variable "redis_auto_minor_version_upgrade" {
  description = <<-EOT
    Whether ElastiCache can apply minor engine upgrades automatically during the
    configured maintenance window.

    Enabled by default so security/stability patching can proceed without forcing
    manual emergency work, while still avoiding surprise daytime changes.
  EOT
  type    = bool
  default = true
}

variable "artifacts_bucket_name" {
  description = "Optional explicit S3 bucket name. Leave null to auto-generate a globally unique name."
  type        = string
  default     = null
  nullable    = true
}

variable "additional_tags" {
  description = "Additional tags applied to all supported AWS resources."
  type        = map(string)
  default     = {}
}