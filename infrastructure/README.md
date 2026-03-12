# Infrastructure Lifecycle

This directory contains the AWS foundation and Kubernetes deployment assets for the production-oriented EKS environment.

## Current Phase Status

### Implemented

- Terraform-managed AWS foundation for the EKS-oriented deployment path
- Kubernetes deploy entrypoint at `infrastructure/k8s/overlays/eks/deploy-production.sh`
- Production-safe destroy entrypoint at `infrastructure/k8s/overlays/eks/destroy-production.sh`
- Strimzi-managed Kafka on EKS, installed and operated through Helm plus Kubernetes manifests
- Helm-managed ALB controller and Milvus deployment steps in the production path

### Partial

- The repository deploys self-managed Kafka on EKS, not Amazon MSK
- Some operational dependencies remain cluster- or operator-provided rather than fully bootstrapped by this repository
- Destroy remains best-effort for certain AWS residuals such as ALBs, retained volumes, ECR images, and versioned S3 objects

### Next Steps

- Add more environment-specific automation only where the repository can also verify cleanup and recovery semantics
- Keep infrastructure docs aligned with the actual EKS and Strimzi deployment model rather than describing alternate Kafka backends

## Deploy and Destroy Symmetry

Production deploy entrypoint:

```bash
./infrastructure/k8s/overlays/eks/deploy-production.sh
```

Equivalent production destroy entrypoint:

```bash
./infrastructure/k8s/overlays/eks/destroy-production.sh --environment production
```

Makefile shortcuts:

```bash
make deploy-production
make destroy-production
```

Extra Terraform flags can be forwarded to destroy:

```bash
make destroy-production DESTROY_ARGS="-- --auto-approve"
```

## Production Deploy Flow

`deploy-production.sh` currently performs the following high-level sequence:

1. `terraform apply`
2. `update-kubeconfig.sh`
3. `sync-ecr-images.sh`
4. immutable image-policy checks
5. `install-alb-controller.sh`
6. Kafka verification
7. `apply-cloud-config.sh`
8. `deploy-milvus.sh`
9. `kubectl apply -k infrastructure/k8s/overlays/eks`
10. wait for workloads and optional data-init completion

That deploy flow provisions resources through more than one mechanism:

- Terraform: VPC, EKS, ElastiCache, ECR, S3, IAM
- Helm: AWS Load Balancer Controller, Milvus, Strimzi operator
- Kustomize / kubectl: application manifests and Strimzi Kafka CRs

The destroy flow deliberately unwinds those layers in the reverse direction instead of assuming Terraform alone will remove everything cleanly.

## Production Destroy Flow

`destroy-production.sh` is intentionally explicit and conservative:

1. require `--environment production`
2. print destructive warnings and require interactive confirmation unless `--yes` is supplied
3. refresh kubeconfig from Terraform outputs when possible
4. delete the public ALB ingress first
5. uninstall Helm-managed Milvus
6. delete Kafka CRs from `infrastructure/k8s/overlays/eks/kafka`
7. wait for the `Kafka` CR to disappear before uninstalling the Strimzi operator
8. uninstall the AWS Load Balancer Controller and attempt best-effort cleanup of its IAM role and policy
9. delete the main EKS application overlay
10. run `terraform destroy`

This order exists to reduce common teardown failures:

- removing the Ingress early gives the ALB controller a chance to clean up AWS load balancers and target groups before the cluster disappears
- deleting Kafka CRs before uninstalling Strimzi avoids orphaning CR finalizers
- uninstalling Helm releases before deleting the namespace avoids racing Helm state with namespace termination

## Safety Controls

- explicit environment selection is mandatory
- the script prints the Terraform directory, AWS account, region, cluster name, and current kube context when available
- interactive confirmation requires both the environment name and a destroy phrase unless `--yes` is used
- Kubernetes teardown and Terraform destroy can be skipped independently when recovering from partial failures:

```bash
./infrastructure/k8s/overlays/eks/destroy-production.sh --environment production --skip-k8s-teardown -- --auto-approve
./infrastructure/k8s/overlays/eks/destroy-production.sh --environment production --skip-terraform
```

## Known Residual Resource Pitfalls

The repository does not guarantee a perfectly clean AWS teardown in every failure mode. Expect to review the following when destroy is interrupted or partially successful:

- ALB and security groups: AWS cleanup is asynchronous after Ingress deletion. The Kubernetes Ingress object may be gone while the ALB, listeners, target groups, and security groups still exist for several minutes.
- PVC and EBS volumes: Kafka is declared with `deleteClaim: false`, so broker and ZooKeeper PVCs can remain by design. Other PVC-backed volumes can also linger behind finalizers or namespace termination.
- S3 object retention: the Terraform bucket is versioned and does not use `force_destroy`, so `terraform destroy` can fail until all current and non-current objects are removed.
- ECR images: ECR repositories are not force-deleted, so repository deletion can fail until images are removed.
- IAM cleanup: the ALB controller IAM role and policy are created outside Terraform. The destroy script attempts to remove them, but it only does best-effort cleanup and warns if deletion fails.

## Recommended Safe Usage

1. Make sure no other operator or CI job is currently deploying to the same cluster.
2. Run the destroy script without `--yes` the first time so the target summary is visible.
3. If `terraform destroy` fails, resolve the reported residual AWS resource and rerun the same command instead of switching to blind force deletion.
4. Keep the final Terraform prompt enabled unless your change-management process requires `--auto-approve`.
