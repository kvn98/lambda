# Migration Guide: Node IAM Policy → IRSA & Ingress Controller → Gateway API

**Author:** CDSAi MLOps Team  
**Status:** In Review  
**Last Updated:** 2026-06  
**Applies To:** All CDSAi-managed EKS workloads migrating to Gateway API

---

## Overview

This document describes the end-to-end process for migrating an application from:

- **Node-level IAM policies** → **IRSA (IAM Roles for Service Accounts)**
- **Kubernetes Ingress resources** → **AWS Gateway API** (with ALB via `gateway.k8s.aws`)

This migration also includes moving the application into a **new namespace** and cleaning up legacy resources. The steps below reflect the pattern established during the `claims-genie` migration and should be followed for all subsequent application migrations.

---

## Prerequisites

Before beginning, confirm the following:

- You have `kubectl` access to the target EKS cluster
- Terraform Cloud workspace access for the relevant environment
- AWS console / CLI access for ACM, ALB, and Route53 operations
- The application's existing IAM node policy ARNs are known
- A target namespace has been decided (e.g., `claims-genie`)

---

## Phase 1 — IRSA Migration (Node Policy → Service Account Role)

### Step 1.1 — Inventory Existing Resources

Identify all AWS resources currently accessed via the node instance profile. This includes:

- S3 buckets
- DynamoDB tables
- Bedrock endpoints
- OpenSearch / AOSS collections
- SSM parameters
- Any other AWS services used by the pod

**Action:** Consolidate all resource ARNs into a single Terraform module or resource block in the CDSAi IaC repo.

### Step 1.2 — Import Existing Resources into Terraform

If the resources already exist (were created manually or outside TF), import them:

```bash
terraform import aws_iam_policy.example arn:aws:iam::ACCOUNT_ID:policy/PolicyName
```

Ensure all resources are state-managed before proceeding.

### Step 1.3 — Create the IRSA IAM Role

Create a new IAM role with a trust policy scoped to the EKS OIDC provider and the target service account:

```hcl
module "irsa_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"

  role_name = "claims-genie-irsa"

  oidc_providers = {
    main = {
      provider_arn               = var.oidc_provider_arn
      namespace_service_accounts = ["claims-genie:claims-genie-sa"]
    }
  }

  role_policy_arns = {
    policy = aws_iam_policy.claims_genie.arn
  }
}
```

### Step 1.4 — Create and Annotate the Kubernetes Service Account

Create the service account in the target namespace with the IRSA role ARN annotation:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: claims-genie-sa
  namespace: claims-genie
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::ACCOUNT_ID:role/claims-genie-irsa
```

### Step 1.5 — Update Deployment to Use the Service Account

Reference the service account in the pod spec:

```yaml
spec:
  serviceAccountName: claims-genie-sa
```

Verify the pod receives credentials:

```bash
kubectl exec -n claims-genie <pod-name> -- aws sts get-caller-identity
```

---

## Phase 2 — Gateway API Migration (Ingress → HTTPRoute + TargetGroupConfiguration)

### Step 2.1 — Create ACM Certificate

Request or identify an existing ACM certificate for the application's domain:

```bash
aws acm request-certificate \
  --domain-name claims-genie-api.dev.mlops.nylcloud.com \
  --validation-method DNS \
  --region us-east-1
```

Confirm validation is complete before proceeding.

### Step 2.2 — Create the Gateway (if not shared)

If using a shared Gateway (recommended), verify the `mlops-dev` Gateway exists in the infra namespace. If creating a new one:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: mlops-gateway
  namespace: infra
spec:
  gatewayClassName: aws
  listeners:
    - name: https
      protocol: HTTPS
      port: 443
      tls:
        certificateRefs:
          - kind: Secret
            name: tls-cert
```

### Step 2.3 — Create TargetGroupConfiguration

This binds the backend Kubernetes Service to an ALB Target Group:

```yaml
apiVersion: gateway.k8s.aws/v1beta1
kind: TargetGroupConfiguration
metadata:
  name: claims-genie-tgc
  namespace: claims-genie
spec:
  targetReference:
    kind: Service
    name: gbs-claims-genie-streaming
  defaultConfiguration:
    healthCheckConfig:
      healthCheckPath: /claims-genie/readiness
      healthCheckProtocol: HTTP
      matcher:
        httpCode: "200-399"
```

> **Note:** Confirm the health check path matches the application's actual readiness endpoint. For streaming services, ensure the backend service name matches exactly.

### Step 2.4 — Create HTTPRoute

Route traffic from the Gateway to the backend service:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: claims-genie-route
  namespace: claims-genie
spec:
  parentRefs:
    - name: mlops-gateway
      namespace: infra
  hostnames:
    - "claims-genie-api.dev.mlops.nylcloud.com"
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /claims-genie
      backendRefs:
        - name: gbs-claims-genie-streaming
          port: 8080
```

### Step 2.5 — Deploy to New Namespace

Launch the application workloads (Deployment, Service, ConfigMaps, etc.) into the new namespace. Do **not** decommission the old namespace yet — run both in parallel during the DNS cutover window.

---

## Phase 3 — DNS Cutover & ALB Certificate Update

### Step 3.1 — Identify the ALB Listener ARN

```bash
aws elbv2 describe-listeners \
  --listener-arns arn:aws:elasticloadbalancing:us-east-1:<ACCOUNT_ID>:listener/app/k8s-infragat-mlopsdev-<SUFFIX> \
  --query 'Listeners[0].Certificates'
```

### Step 3.2 — Attach the New ACM Certificate to the ALB Listener

Once you have the listener ARN:

```bash
aws elbv2 modify-listener \
  --listener-arn arn:aws:elasticloadbalancing:us-east-1:<ACCOUNT_ID>:listener/app/k8s-infragat-mlopsdev-<SUFFIX> \
  --certificates CertificateArn=arn:aws:acm:us-east-1:<ACCOUNT_ID>:certificate/<CERT_ID>
```

> **Reference:** This step was identified during `claims-genie` migration — the ALB listener must be updated manually (or via IaC) whenever a new ACM cert is issued, as the Gateway controller does not automatically rotate listener certificates.

### Step 3.3 — Update Route53 A Record

Update the DNS A record to point to the new ALB DNS name:

1. Navigate to Route53 → Hosted Zone → `dev.mlops.nylcloud.com`
2. Update the A record for `claims-genie-api` to alias the new ALB
3. Confirm propagation:

```bash
nslookup claims-genie-api.dev.mlops.nylcloud.com
# or
dig claims-genie-api.dev.mlops.nylcloud.com
```

Validate the app is reachable at the new URL (e.g., `http://claims-genie-api.dev.mlops.nylcloud.com/docs#`).

---

## Phase 4 — Cleanup

Once traffic is confirmed flowing through the new Gateway API path and DNS has propagated:

### Step 4.1 — Remove Old Ingress Resources

```bash
kubectl delete ingress <old-ingress-name> -n <old-namespace>
```

### Step 4.2 — Detach and Delete Old Node IAM Policies

Remove the permissions from the node instance profile (or the old managed policy) in Terraform, and `terraform apply`.

### Step 4.3 — Delete Old Namespace (if applicable)

```bash
kubectl delete namespace <old-namespace>
```

Ensure no remaining resources (PVCs, secrets) are needed before deletion.

### Step 4.4 — Update Terraform State

Confirm all old resources are removed from state:

```bash
terraform state list | grep <old-resource-names>
terraform state rm <resource> # if orphaned
```

---

## Validation Checklist

| Check | Command / Method |
|---|---|
| Pod uses IRSA identity | `kubectl exec -- aws sts get-caller-identity` |
| TargetGroupConfiguration exists | `kubectl get targetgroupconfiguration -n <ns> -o yaml` |
| Health check passing | AWS Console → Target Groups → Targets |
| HTTPRoute attached to Gateway | `kubectl describe httproute -n <ns>` |
| DNS resolving | `nslookup <hostname>` |
| App accessible via new URL | `curl https://<hostname>/docs` |
| Old Ingress removed | `kubectl get ingress -A` |
| Old node policy detached | AWS Console → EC2 → Node Instance Profile |

---

## Reference PRs

- Initial `TargetGroupConfiguration` setup: `https://git.nylcloud.com/CDSAi/mlops-services-alb/pull/237`
- ALB cert + listener update: `https://git.nylcloud.com/CDSAi/mlops-services-alb/pull/238`

---

## Notes

- The `claims-genie` migration used a **streaming service** (`gbs-claims-genie-streaming`) as the backend — confirm the service type and port for each new application.
- The `TargetGroupConfiguration` CRD is part of the `gateway.k8s.aws` controller — ensure the controller version supports `v1beta1` in the target cluster.
- ACM certificate propagation can take several minutes. Do not update the ALB listener until the cert status shows `Issued`.
- Dan (Rice, Daniel A.) is the primary reviewer for Gateway API changes. PRs touching ALB/listener config should be reviewed before applying to `stage` or `prod`.
