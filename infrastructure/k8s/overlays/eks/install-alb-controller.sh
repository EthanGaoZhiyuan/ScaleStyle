#!/usr/bin/env bash

set -euo pipefail

TF_DIR=${TF_DIR:-infrastructure/terraform}
NAMESPACE=kube-system
SA_NAME=aws-load-balancer-controller

cluster_name=${CLUSTER_NAME:-$(terraform -chdir="$TF_DIR" output -raw cluster_name)}
aws_region=${AWS_REGION:-$(terraform -chdir="$TF_DIR" output -raw aws_region)}
vpc_id=${VPC_ID:-$(terraform -chdir="$TF_DIR" output -raw vpc_id)}
account_id=${AWS_ACCOUNT_ID:-$(terraform -chdir="$TF_DIR" output -raw aws_account_id)}

policy_name="AWSLoadBalancerControllerIAMPolicy-${cluster_name}"
role_name="AmazonEKSLoadBalancerControllerRole-${cluster_name}"

tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT

curl -fsSL \
  https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v2.11.0/docs/install/iam_policy.json \
  -o "$tmp_dir/iam_policy.json"

policy_arn=$(aws iam list-policies --scope Local --query "Policies[?PolicyName=='${policy_name}'].Arn | [0]" --output text)
if [[ "$policy_arn" == "None" ]]; then
  policy_arn=$(aws iam create-policy \
    --policy-name "$policy_name" \
    --policy-document "file://$tmp_dir/iam_policy.json" \
    --query 'Policy.Arn' --output text)
fi

oidc_issuer=$(aws eks describe-cluster --region "$aws_region" --name "$cluster_name" --query 'cluster.identity.oidc.issuer' --output text)
oidc_provider=${oidc_issuer#https://}

if ! aws iam get-role --role-name "$role_name" >/dev/null 2>&1; then
  cat > "$tmp_dir/trust-policy.json" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::${account_id}:oidc-provider/${oidc_provider}"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "${oidc_provider}:aud": "sts.amazonaws.com",
          "${oidc_provider}:sub": "system:serviceaccount:${NAMESPACE}:${SA_NAME}"
        }
      }
    }
  ]
}
EOF

  aws iam create-role --role-name "$role_name" --assume-role-policy-document "file://$tmp_dir/trust-policy.json" >/dev/null
fi

aws iam attach-role-policy --role-name "$role_name" --policy-arn "$policy_arn" >/dev/null 2>&1 || true

role_arn="arn:aws:iam::${account_id}:role/${role_name}"

kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${SA_NAME}
  namespace: ${NAMESPACE}
  annotations:
    eks.amazonaws.com/role-arn: ${role_arn}
EOF

helm repo add eks https://aws.github.io/eks-charts >/dev/null 2>&1 || true
helm repo update >/dev/null

helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n "$NAMESPACE" \
  --set clusterName="$cluster_name" \
  --set serviceAccount.create=false \
  --set serviceAccount.name="$SA_NAME" \
  --set region="$aws_region" \
  --set vpcId="$vpc_id" \
  --wait \
  --timeout 10m

kubectl rollout status deployment/aws-load-balancer-controller -n "$NAMESPACE" --timeout=300s