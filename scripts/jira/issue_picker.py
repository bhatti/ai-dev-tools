"""Pick Jira issues labeled ai-ready, transition them to ai-in-progress.

Usage:
    python -m scripts.jira.issue_picker
    python -m scripts.jira.issue_picker --issue-id PROJ-42   # pick a specific issue

Required env: JIRA_PROJECT, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_BASE_URL
Optional env: PICKUP_LABEL, INPROGRESS_LABEL, MAX_ISSUES,
              BITBUCKET_WORKSPACE, BITBUCKET_REPO

Output: /workspace/issue.json (per-pod emptyDir; one issue per pod)
Exit codes: 0=picked at least one, 2=none available, 1=error

Can be called standalone, from a K8s CronJob, or from a formicary SHELL task.
"""

import re
import subprocess
import sys

import click

from scripts.common.artifacts import write_json
from scripts.common.config import load_config
from scripts.common.jira_api import get_issue, search_issues
from scripts.common.label_utils import jira_transition_label


def parse_repo_from_labels(
    labels: list[str], default_workspace: str, default_repo: str, default_branch: str = "main"
) -> tuple[str, str, str]:
    """Extract repo and branch from 'repo:<repo>:<branch>' label.

    Workspace always comes from env (BITBUCKET_WORKSPACE). Format: repo:<repo>:<branch>
    """
    for label in labels:
        m = re.match(r"repo:([^:]+)(?::(.+))?", label)
        if m:
            repo = m.group(1) or default_repo
            branch = m.group(2) or default_branch
            return default_workspace, repo, branch
    return default_workspace, default_repo, default_branch


def pick_issue(config: dict, raw: dict) -> bool:
    """Transition one issue from pickup label → inprogress and write issue.json.

    Returns True on success.
    """
    issue_key = raw.get("key", "")
    if not issue_key:
        return False

    fields = raw.get("fields", {})
    labels: list[str] = fields.get("labels", [])
    default_workspace = config.get("BITBUCKET_WORKSPACE", "")
    default_repo = config.get("BITBUCKET_REPO", "")
    default_branch = config.get("BASE_BRANCH", "main")
    workspace, repo, branch = parse_repo_from_labels(labels, default_workspace, default_repo, default_branch)

    title = fields.get("summary", "")
    body = fields.get("description", "") or ""

    print(f"Picking {issue_key}: {title}")
    write_json(config, issue_key, "issue.json", {
        "id": issue_key,
        "key": issue_key,
        "title": title,
        "body": body,
        "url": f"{config['JIRA_BASE_URL']}/browse/{issue_key}",
        "project": config["JIRA_PROJECT"],
        "bitbucket_workspace": workspace,
        "bitbucket_repo": repo,
        "base_branch": branch,
        "labels": labels,
        "source": "jira",
    })
    print(f"  Written to workspace/issue.json")
    jira_transition_label(config, issue_key, config["PICKUP_LABEL"], config["INPROGRESS_LABEL"])
    print(f"::set-output name=IssueNumber::{issue_key}")
    print(f"::set-output name=IssueTitle::{title}")
    print(f"::set-output name=IssueURL::{config['JIRA_BASE_URL']}/browse/{issue_key}")
    print(f"::set-output name=BitbucketWorkspace::{workspace}")
    print(f"::set-output name=BitbucketRepo::{repo}")
    return True


@click.command()
@click.option(
    "--issue-id",
    default=None,
    help="Pick a specific Jira issue key (e.g. PROJ-42). "
         "If omitted, searches for all issues with the pickup label.",
)
def main(issue_id: str | None) -> None:
    config = load_config(required=["JIRA_PROJECT", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_BASE_URL"])

    if issue_id:
        # Single-issue mode — used by K8s single-step template or formicary
        raw = get_issue(config, issue_id)
        if not raw:
            print(f"ERROR: Jira issue {issue_id} not found", file=sys.stderr)
            sys.exit(1)
        success = pick_issue(config, raw)
        sys.exit(0 if success else 1)

    # Batch mode — pick up to MAX_ISSUES ready issues
    max_issues = int(config["MAX_ISSUES"])
    jql = (
        f'project = "{config["JIRA_PROJECT"]}" '
        f'AND labels = "{config["PICKUP_LABEL"]}" '
        f'AND status != Done'
    )
    issues = search_issues(config, jql, max_results=20)
    if not issues:
        print(f"No issues found with label '{config['PICKUP_LABEL']}'")
        sys.exit(2)

    import json as _json
    picked = 0
    issues_json_list = []
    for raw in issues[:max_issues]:
        if pick_issue(config, raw):
            picked += 1
            issue_key = raw.get("key", "")
            fields = raw.get("fields", {})
            title = fields.get("summary", "")
            default_workspace = config.get("BITBUCKET_WORKSPACE", "")
            default_repo = config.get("BITBUCKET_REPO", "")
            workspace, repo, branch = parse_repo_from_labels(
                fields.get("labels", []), default_workspace, default_repo
            )
            issues_json_list.append({
                "IssueNumber": issue_key,
                "IssueTitle": title,
                "IssueURL": f"{config['JIRA_BASE_URL']}/browse/{issue_key}",
                "BitbucketWorkspace": workspace,
                "BitbucketRepo": repo,
                "_description": f"{issue_key}: {title}",
                "_user_key": issue_key,
            })
            _launch_pipeline(issue_key)

    if issues_json_list:
        print(f"::set-output name=IssuesJSON::{_json.dumps(issues_json_list)}")
    print(f"Picked {picked} issue(s)")
    sys.exit(0 if picked > 0 else 2)


def _launch_pipeline(issue_id: str) -> None:
    """Launch K8s pipeline Job for this issue (no-op outside cluster). Raises on failure."""
    if not issue_id:
        return
    result = subprocess.run(
        [sys.executable, "-m", "scripts.jira.launch_pipeline", "--issue-id", issue_id],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"launch_pipeline failed for issue {issue_id} (exit {result.returncode})")


if __name__ == "__main__":
    main()
