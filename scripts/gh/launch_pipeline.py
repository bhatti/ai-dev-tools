"""Launch a K8s pipeline Job for a picked GitHub issue.

Usage:
    python -m scripts.gh.launch_pipeline --issue-id 42

Called by issue_picker.py after successfully writing issue.json.
Uses kubectl to apply the pipeline job template with the correct ISSUE_ID.

Skipped automatically when not running inside a Kubernetes cluster
(KUBERNETES_SERVICE_HOST not set) — safe to call locally too.

Required env (in-cluster): kubectl must be available and the pod's
ServiceAccount must have the ai-agent-job-creator Role (see k8s/rbac.yaml).

Optional env:
    PIPELINE_JOB_TEMPLATE  — path to the pipeline Job YAML template
                              (default: /app/k8s/gh-pipeline-job.yaml)
    K8S_NAMESPACE          — namespace to create the Job in (default: ai-dev)
"""

import os
import subprocess
import sys

import click


def in_cluster() -> bool:
    """Return True when running inside a Kubernetes pod."""
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def launch_job(issue_id: str, namespace: str, template_path: str) -> bool:
    """Render and apply the pipeline Job template. Returns True on success."""
    env = os.environ.copy()
    env["ISSUE_ID"] = str(issue_id)

    # Render template with envsubst, pipe to kubectl apply
    try:
        envsubst = subprocess.run(
            ["envsubst", "<", template_path],
            env=env,
            capture_output=True,
            text=True,
            shell=False,
        )
        # envsubst with redirects needs shell=True; use heredoc style instead
        envsubst = subprocess.run(
            f"envsubst < {template_path}",
            env=env,
            capture_output=True,
            text=True,
            shell=True,
        )
        if envsubst.returncode != 0:
            print(f"ERROR: envsubst failed: {envsubst.stderr.strip()}", file=sys.stderr)
            return False

        kubectl = subprocess.run(
            ["kubectl", "apply", "-n", namespace, "-f", "-"],
            input=envsubst.stdout,
            capture_output=True,
            text=True,
        )
        if kubectl.returncode != 0:
            print(f"ERROR: kubectl apply failed: {kubectl.stderr.strip()}", file=sys.stderr)
            return False

        print(f"Launched pipeline Job for issue {issue_id} in namespace {namespace}")
        return True
    except FileNotFoundError as e:
        print(f"ERROR: {e} — is kubectl/envsubst installed?", file=sys.stderr)
        return False


@click.command()
@click.option("--issue-id", required=True, help="GitHub issue number")
def main(issue_id: str) -> None:
    if not in_cluster():
        print(f"Not running in-cluster — skipping Job launch for issue {issue_id}")
        sys.exit(0)

    namespace = os.environ.get("K8S_NAMESPACE", "ai-dev")
    template = os.environ.get("PIPELINE_JOB_TEMPLATE", "/app/k8s/gh-pipeline-job.yaml")

    if not os.path.exists(template):
        print(f"WARNING: template {template} not found — skipping Job launch", file=sys.stderr)
        sys.exit(0)

    success = launch_job(issue_id, namespace, template)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
