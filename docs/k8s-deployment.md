# Kubernetes Deployment Guide

## Overview

The AI workflow runs as K8s Jobs (one per issue) and a CronJob for issue picking. All pods share a PersistentVolumeClaim at `/workspace` for artifact passing between steps.

## Prerequisites

- A Kubernetes cluster (local: kind, minikube; cloud: EKS, GKE, AKS)
- `kubectl` configured for your cluster
- A container registry (default: `ghcr.io/bhatti/ai-dev-tools`)
- A `ReadWriteMany` storage class (NFS, EFS, Azure Files, etc.)

## Step-by-Step Deployment

### 1. Create namespace

```bash
kubectl apply -f k8s/namespace.yaml
```

### 2. Create workspace PVC

Edit `k8s/pvc.yaml` to set your storage class if needed:

```yaml
storageClassName: nfs   # or efs, azurefile, etc.
```

Then apply:

```bash
kubectl apply -f k8s/pvc.yaml
```

### 3. Create secrets

```bash
cp k8s/secrets.yaml.example k8s/secrets.yaml
# Edit with your real tokens
kubectl apply -f k8s/secrets.yaml
```

Never commit `k8s/secrets.yaml`. Add it to `.gitignore`.

### 4. Deploy issue picker CronJobs

```bash
# GitHub CronJob (runs every 5 minutes)
kubectl apply -f k8s/gh-issue-picker-cron.yaml

# Jira CronJob (optional)
kubectl apply -f k8s/jira-issue-picker-cron.yaml
```

Edit the CronJobs to set `GH_ORG`, `GH_REPO`, `JIRA_PROJECT`, etc. before applying.

### 5. Run a full pipeline job

The issue picker writes `issue.json` artifacts but does NOT automatically launch pipeline jobs. You need to either:

**Option A**: Launch manually after picker runs:
```bash
# Get the picked issue ID
ISSUE_ID=$(ls test-workspace/ | head -1)

# Launch pipeline job
ISSUE_ID=$ISSUE_ID GH_ORG=myorg GH_REPO=myrepo \
  envsubst < k8s/gh-pipeline-job.yaml | kubectl apply -f -
```

**Option B**: Build an orchestrator that watches the workspace PVC and launches jobs when new `issue.json` files appear (future enhancement).

### 6. Run a single step for debugging

```bash
# Run just the plan step for issue #42
STEP=plan ISSUE_ID=42 GH_ORG=myorg GH_REPO=myrepo \
  envsubst < k8s/single-step-job.yaml | kubectl apply -f -

# Watch logs
kubectl logs -f -n ai-dev job/ai-step-plan-42
```

## Networking

The scripts only make outbound API calls (GitHub, Jira, Claude/Bedrock). No inbound ports are needed.

If `ANTHROPIC_BEDROCK_BASE_URL` points to an internal service (e.g. `http://ai/bedrock`), ensure the pods can reach it via cluster DNS. Use a Service or Ingress in the same namespace.

For air-gapped environments, ensure:
- Container image is in a private registry
- `you-got-skills` clone can reach `github.com` (or pre-build with skills installed)

## Local Testing with kind

```bash
# Create a kind cluster
kind create cluster --name ai-dev

# Load local image into kind
docker build -t ai-dev-tools:local .
kind load docker-image ai-dev-tools:local --name ai-dev

# Use hostPath for PVC (kind doesn't have ReadWriteMany by default)
# Edit k8s/pvc.yaml to use standard storage class and ReadWriteOnce for single-node testing

kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/pvc.yaml
kubectl apply -f k8s/secrets.yaml   # Your real secrets

# Run a single step
STEP=issue_picker ISSUE_ID=0 GH_ORG=myorg GH_REPO=myrepo \
  envsubst < k8s/single-step-job.yaml | kubectl apply -f -
```

## Monitoring

```bash
# List all ai-dev jobs
kubectl get jobs -n ai-dev

# Watch a running pipeline
kubectl get pods -n ai-dev -w

# Tail logs for a step
kubectl logs -n ai-dev -l job-name=ai-gh-implement-42 -f

# Inspect artifacts
kubectl exec -n ai-dev -it <pod-name> -- ls /workspace/42/
```

## Cleanup

```bash
# Delete completed jobs older than 24h (set ttlSecondsAfterFinished in job spec)
kubectl delete jobs -n ai-dev --field-selector status.successful=1

# Delete everything
kubectl delete namespace ai-dev
```
