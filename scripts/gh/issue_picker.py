"""Pick GitHub issues labeled ai-ready, transition them to ai-in-progress.

Usage:
    python -m scripts.gh.issue_picker
    python -m scripts.gh.issue_picker --issue-id 42   # pick a specific issue

Required env: GH_ORG, GH_REPO, GH_TOKEN
Optional env: PICKUP_LABEL, INPROGRESS_LABEL, MAX_ISSUES

Output: /workspace/{issue_number}/issue.json for each picked issue
Exit codes: 0=picked at least one, 2=none available, 1=error

Can be called standalone, from a K8s CronJob, or from a formicary SHELL task.
When called via the single-step K8s template, --issue-id may be passed as a
no-op (issue_picker ignores it and runs in batch mode).
"""

import json
import subprocess
import sys

import click


from scripts.common.artifacts import write_json
from scripts.common.config import load_config
from scripts.common.label_utils import gh_transition_label


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


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
    return json.loads(result.stdout)


def pick_issue(config: dict, issue: dict) -> bool:
    """Transition label and write issue.json. Returns True on success."""
    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    base_branch = config.get("BASE_BRANCH", "main")
    issue_id = str(issue["number"])
    label_names = [l["name"] for l in issue.get("labels", [])]

    print(f"Picking issue #{issue_id}: {issue['title']}")
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
    print(f"  Written to workspace/{issue_id}/issue.json")
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
    help="Ignored by issue_picker (it always runs in batch mode). "
         "Accepted so it can be called from the K8s single-step template "
         "without error.",
)
def main(issue_id: str | None) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO", "GH_TOKEN"])
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
