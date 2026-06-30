# Formicary Integration Guide

## Overview

The same Python scripts that run in Kubernetes can be invoked directly from
[formicary](https://github.com/bhatti/formicary), a workflow orchestration
framework. No code changes are needed — formicary YAML job definitions simply
call `python -m scripts.<module> --issue-id <ID>` as DOCKER tasks.

## Execution Models

All three execution models use identical Python scripts and produce identical
file-based artifacts under `/workspace/{issue-id}/`:

| Model | Entry point | Scheduling |
|-------|-------------|------------|
| **Standalone / local** | `python -m scripts.gh.plan --issue-id 42` | Manual |
| **Kubernetes** | init-container pipeline Job + CronJob | K8s scheduler |
| **Formicary** | `formicary submit formicary/ai-gh-implement.yaml --var ISSUE_ID=42` | Formicary cron or trigger |

## Available Job Definitions

| File | Purpose |
|------|---------|
| `formicary/ai-gh-issue-picker.yaml` | Cron: pick `ai-ready` GitHub issues every 5 min |
| `formicary/ai-gh-implement.yaml` | Pipeline: plan → implement → create_pr → monitor_pr (calls learn on exit) |
| `formicary/ai-jira-issue-picker.yaml` | Cron: pick `ai-ready` Jira issues every 5 min |
| `formicary/ai-jira-implement.yaml` | Pipeline: plan → implement → create_pr → monitor_pr (calls learn on exit) |

## Deployment

### Prerequisites

1. Build and push the Docker image:
   ```bash
   make build push IMAGE=<your-registry>/ai-dev-tools
   ```

2. Create a formicary config for your secrets (or use formicary's vault/secret
   support). Minimum required env vars are the same as for Kubernetes — see
   [docs/configuration.md](configuration.md).

### Submit jobs

```bash
# One-time: pick any ready GitHub issues
formicary submit formicary/ai-gh-issue-picker.yaml

# Pipeline for a specific GitHub issue
formicary submit formicary/ai-gh-implement.yaml --var ISSUE_ID=42

# Pipeline for a specific Jira issue
formicary submit formicary/ai-jira-implement.yaml --var ISSUE_ID=PROJ-42
```

### Deploy as a cron job

```bash
# Register the issue pickers as scheduled formicary jobs
formicary job create formicary/ai-gh-issue-picker.yaml
formicary job create formicary/ai-jira-issue-picker.yaml
```

## Connecting issue picker → pipeline

When running in Kubernetes, `issue_picker.py` automatically calls
`launch_pipeline.py` which uses `kubectl apply` to launch the pipeline Job for
each picked issue (requires the `ai-agent` ServiceAccount — see `k8s/rbac.yaml`).

In formicary, the preferred approach is to use formicary's **job-trigger**
feature: configure the issue picker job to emit an event that triggers the
implement pipeline with the picked `ISSUE_ID` variable. Example trigger config:

```yaml
# In ai-gh-issue-picker.yaml, add to the pick-issues task:
on-completed:
  - job-type: ai-gh-implement
    variables:
      ISSUE_ID: "{{.ArtifactIDs.pick-issues.issue_id}}"
```

Alternatively, you can chain them manually or use a wrapper script.

## Artifact handoff

Each step reads its inputs from and writes its outputs to
`/workspace/{issue-id}/`. When running in formicary, mount a shared PVC (or
configure artifact storage) so each task in the pipeline can access the files
written by the previous task.

Formicary's built-in artifact support (with `artifacts.paths`) handles this
automatically when using Docker tasks with a shared volume mount.

## Environment Variables

See [docs/configuration.md](configuration.md) for the full variable reference.
The formicary YAML files use `${VAR}` substitution — set these in formicary's
job variable store or inject via your CI/CD pipeline.

## Troubleshooting

- **Task logs**: `formicary job logs <job-id>` — each task streams its stdout/stderr
- **Artifacts**: `formicary artifact download <job-id>` — retrieves `/workspace/{issue-id}/`
- **Re-running a step**: Since all steps are idempotent (check for existing
  output before running), you can safely re-submit a job after a failure. It
  will skip completed steps and resume from the first incomplete one.
