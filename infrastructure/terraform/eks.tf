module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = var.cluster_name
  cluster_version = var.cluster_version

  cluster_endpoint_public_access           = true
  enable_cluster_creator_admin_permissions = true
  enable_irsa                              = true

  vpc_id                   = module.vpc.vpc_id
  subnet_ids               = module.vpc.private_subnets
  control_plane_subnet_ids = module.vpc.private_subnets

  cluster_addons = {
    coredns    = {}
    kube-proxy = {}
    vpc-cni    = {}
  }

  eks_managed_node_group_defaults = {
    ami_type       = "AL2_x86_64"
    instance_types = var.node_instance_types
    disk_size      = var.node_disk_size
  }

  eks_managed_node_groups = {
    cpu = {
      name           = "${var.cluster_name}-cpu"
      min_size       = var.node_min_size
      desired_size   = var.node_desired_size
      max_size       = var.node_max_size
      capacity_type  = "ON_DEMAND"
      subnet_ids     = module.vpc.private_subnets

      labels = {
        workload = "general"
        profile  = "cpu"
      }

      tags = {
        Name = "${var.cluster_name}-cpu"
      }
    }
  }

  tags = local.common_tags
}