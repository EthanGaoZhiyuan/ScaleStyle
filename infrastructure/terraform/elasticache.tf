resource "aws_security_group" "elasticache_redis" {
  name        = "${local.name_prefix}-redis"
  description = "Allow EKS worker nodes to reach ScaleStyle ElastiCache Redis"
  vpc_id      = module.vpc.vpc_id

  # Only EKS worker node ENIs may reach Redis — no broad VPC CIDR rule.
  ingress {
    description     = "Redis from EKS node security group"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-redis-sg"
  })
}

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${local.name_prefix}-redis-subnets"
  subnet_ids = module.vpc.private_subnets

  tags = local.common_tags
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = replace("${local.name_prefix}-redis", "_", "-")
  description                = "ScaleStyle Redis Cache"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  port                       = 6379
  parameter_group_name       = "default.redis7"
  auto_minor_version_upgrade = var.redis_auto_minor_version_upgrade

  # HA: primary + 1 replica across two AZs with automatic failover.
  # ⚠️  CRITICAL: DO NOT enable cluster_mode (sharding). The event-consumer Lua
  #    script performs atomic writes across multiple key prefixes (user:*, item:*,
  #    global:*, popularity:*) that hash to different cluster slots. Cluster mode
  #    would cause CROSSSLOT errors on every event, resulting in 100% DLQ rate.
  #    See event-consumer/src/consumer.py class EventConsumer docstring for details.
  automatic_failover_enabled = true
  multi_az_enabled           = true
  num_cache_clusters         = 2

  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [aws_security_group.elasticache_redis.id]

  # Encryption at rest was already on; enable in-transit TLS.
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true

  # Retain daily snapshots for point-in-time recovery and keep both maintenance
  # and backup windows explicit so stateful changes happen on an operator-chosen schedule.
  snapshot_retention_limit   = var.redis_snapshot_retention_limit
  snapshot_window            = var.redis_snapshot_window
  preferred_maintenance_window = var.redis_preferred_maintenance_window

  # Production-safe default: defer stateful changes to the maintenance window
  # unless an environment explicitly opts into immediate apply.
  apply_immediately = var.redis_apply_immediately

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-redis"
  })
}