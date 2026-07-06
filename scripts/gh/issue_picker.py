"""Pick GitHub issues labeled ai-ready, transition them to ai-in-progress.

Usage:
    python -m scripts.gh.issue_picker
    python -m scripts.gh.issue_picker --issue-id 42   # fetch a specific issue

Required env: GH_ORG, GH_REPO, GH_TOKEN
Optional env: PICKUP_LABEL, INPROGRESS_LABEL, MAX_ISSUES

Output: /workspace/issue.json (per-pod emptyDir; one issue per pod)
Exit codes: 0=picked at least one, 2=none available, 1=error

With --issue-id: fetches that specific issue and writes issue.json (no label check).
Without --issue-id: batch mode, searches for issues with the pickup label.
"""

import json
import subprocess
import sys

import click

from scripts.common.artifacts import write_json
from scripts.common.config import load_config
from scripts.common.label_utils import gh_transition_label
from scripts.common.shell import run_cmd as _run


def fetch_issue(config: dict, issue_number: str) -> dict | None:
    """Fetch a single issue by number. Returns None if not found or on parse error."""
    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    result = _run([
        "gh", "issue", "view", issue_number,
        "-R", f"{org}/{repo}",
        "--json", "number,title,body,url,labels",
    ])
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse gh output for issue {issue_number}: {e}", file=sys.stderr)
        return None


def fetch_ready_issues(config: dict) -> list[dict]:
    """List open issues with the pickup label."""
    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    label = config["PICKUP_LABEL"]
    result = _run([
        "gh", "issue", "list",
        "-R", f"{org}/{repo}",
        "--label", label,
        "--state", "open",
        "--limit", "20",
        "--json", "number,title,body,url,labels",
    ])
    if not result.stdout.strip():
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse gh issue list output: {e}", file=sys.stderr)
        return []


def pick_issue(config: dict, issue: dict, transition_label: bool = True) -> bool:
    """Write issue.json and optionally transition the pickup label to in-progress."""
    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    base_branch = config.get("BASE_BRANCH", "main")
    issue_id = str(issue["number"])
    label_names = [l["name"] for l in issue.get("labels", [])]

    write_json(config, issue_id, "issue.json", {
        "id": issue_id,
        "number": issue["number"],
        "title": issue["title"],
        "body": issue.get("body", ""),
        "url": issue["url"],
        "org": org,
        "repo": repo,
        "base_branch": base_branch,
        "labels": label_names,
    })
    print(f"  Written to workspace/issue.json")
    if transition_label:
        gh_transition_label(org, repo, issue_id, config["PICKUP_LABEL"], config["INPROGRESS_LABEL"])
    print(f"::set-output name=IssueNumber::{issue_id}")
    print(f"::set-output name=IssueTitle::{issue['title']}")
    print(f"::set-output name=IssueURL::{issue['url']}")
    print(f"::set-output name=GHOrg::{org}")
    print(f"::set-output name=GHRepo::{repo}")
    return True


def launch_pipeline(issue_id: str) -> None:
    """Launch K8s pipeline Job for this issue (no-op outside cluster). Raises on failure."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.gh.launch_pipeline", "--issue-id", issue_id],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"launch_pipeline failed for issue {issue_id} (exit {result.returncode})")


@click.command()
@click.option(
    "--issue-id",
    default=None,
    help="Fetch a specific issue by number and write issue.json (no label check). "
         "If omitted, searches for all issues with the pickup label.",
)
def main(issue_id: str | None) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO", "GH_TOKEN"])

    if issue_id:
        # Single-issue mode: fetch by number, write issue.json.
        # Label is already transitioned by the issue-picker cron job; don't touch it here.
        raw = fetch_issue(config, issue_id)
        if not raw:
            print(f"ERROR: GitHub issue {issue_id} not found", file=sys.stderr)
            sys.exit(1)
        print(f"Picking issue #{issue_id}: {raw.get('title', '')}")
        pick_issue(config, raw, transition_label=False)
        sys.exit(0)

    # Batch mode
    print(
        f"[issue_picker] org={config['GH_ORG']} repo={config['GH_REPO']}"
        f" pickup_label={config['PICKUP_LABEL']} max={config['MAX_ISSUES']}",
        flush=True,
    )
    max_issues = int(config["MAX_ISSUES"])

    issues = fetch_ready_issues(config)
    if not issues:
        print(f"No issues found with label '{config['PICKUP_LABEL']}'")
        sys.exit(2)

    picked = 0
    issues_json_list = []
    for issue in issues[:max_issues]:
        if pick_issue(config, issue):
            picked += 1
            issue_id_str = str(issue["number"])
            issues_json_list.append({
                "IssueNumber": issue_id_str,
                "IssueTitle": issue["title"],
                "IssueURL": issue["url"],
                "GHOrg": config["GH_ORG"],
                "GHRepo": config["GH_REPO"],
                "_description": f"#{issue_id_str}: {issue['title']}",
                "_user_key": issue_id_str,
            })
            launch_pipeline(issue_id_str)

    if issues_json_list:
        print(f"::set-output name=IssuesJSON::{json.dumps(issues_json_list)}")
    print(f"Picked {picked} issue(s)")
    sys.exit(0 if picked > 0 else 2)


if __name__ == "__main__":
    main()
