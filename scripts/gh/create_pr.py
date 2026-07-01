"""Push branch and create a GitHub Pull Request.

Usage:
    python -m scripts.gh.create_pr --issue-id 42

Required env: GH_ORG, GH_REPO, GH_TOKEN
Reads:  /workspace/{issue_id}/impl_result.json
        /workspace/{issue_id}/plan_result.json
Writes: /workspace/{issue_id}/pr.json

Idempotent: skips if pr.json already contains a valid URL.
Exit codes: 0=success, 1=error
"""

import json
import re
import sys
from pathlib import Path

import click

from scripts.common.artifacts import read_json, write_json
from scripts.common.config import get_issue_dir, load_config
from scripts.common.git_utils import current_branch, push_branch
from scripts.common.label_utils import gh_transition_label
from scripts.common.shell import run_cmd as _run


def create_github_pr(
    org: str,
    repo: str,
    issue: dict,
    plan_result: dict,
    impl_result: dict,
    branch: str,
) -> dict:
    """Create a PR via gh CLI and return {url, number}."""
    issue_id = issue["number"]
    title = f"[AI] #{issue_id}: {issue['title']}"

    body_lines = [
        f"Closes #{issue_id}",
        "",
        f"## Summary",
        plan_result.get("summary", "AI-generated implementation"),
        "",
        f"## Implementation Details",
        f"- Commits: {impl_result.get('commits', 'unknown')}",
        f"- Tests: {impl_result.get('tests_status', 'unknown')}",
        f"- Complexity: {plan_result.get('total_complexity', 'unknown')}",
        "",
        "_This PR was created by an AI agent._",
    ]
    body = "\n".join(body_lines)

    result = _run([
        "gh", "pr", "create",
        "-R", f"{org}/{repo}",
        "--title", title,
        "--body", body,
        "--head", branch,
    ])
    m = re.search(r'https://github\.com/[^\s]+/pull/(\d+)', result.stdout)
    if not m:
        raise RuntimeError(f"Could not parse PR URL from gh output: {result.stdout.strip()!r}")
    pr_url = m.group(0)
    pr_number = int(m.group(1))
    return {"url": pr_url, "number": pr_number}


@click.command()
@click.option("--issue-id", required=True, help="Issue number")
def main(issue_id: str) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO", "GH_TOKEN"])
    print(f"[create_pr] issue={issue_id} org={config['GH_ORG']} repo={config['GH_REPO']}", flush=True)

    # Idempotency check
    existing = read_json(config, issue_id, "pr.json")
    if existing and existing.get("url"):
        print(f"PR already exists: {existing['url']}")
        sys.exit(0)

    issue = read_json(config, issue_id, "issue.json")
    impl_result = read_json(config, issue_id, "impl_result.json")
    plan_result = read_json(config, issue_id, "plan_result.json") or {}

    if not issue:
        print("ERROR: Missing issue.json", file=sys.stderr)
        sys.exit(1)
    if not impl_result:
        print("ERROR: Missing impl_result.json — did implement step complete?", file=sys.stderr)
        sys.exit(1)
    if impl_result.get("status") != "DONE":
        print(f"ERROR: impl_result status={impl_result.get('status')}: {impl_result.get('reason', '')}", file=sys.stderr)
        sys.exit(1)

    issue_dir = get_issue_dir(config, issue_id)
    repo_dir = issue_dir / "repo"
    branch = impl_result.get("branch") or current_branch(repo_dir)

    org = config["GH_ORG"]
    repo = config["GH_REPO"]

    print(f"Pushing branch {branch}")
    push_branch(repo_dir, branch)

    print("Creating PR")
    pr_info = create_github_pr(org, repo, issue, plan_result, impl_result, branch)

    # Transition label
    gh_transition_label(org, repo, issue_id, config["INPROGRESS_LABEL"], config["PR_OPEN_LABEL"])

    pr_data = {
        "url": pr_info["url"],
        "number": pr_info["number"],
        "branch": branch,
        "issue_id": issue_id,
    }
    write_json(config, issue_id, "pr.json", pr_data)

    print(f"PR created: {pr_info['url']}")
    print(f"::set-output name=PRUrl::{pr_info['url']}")
    print(f"::set-output name=PRNumber::{pr_info['number']}")
    print(f"::set-output name=BranchName::{branch}")
    sys.exit(0)


if __name__ == "__main__":
    main()
