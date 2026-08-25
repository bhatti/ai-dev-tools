"""Create a GitHub Pull Request via gh CLI (branch already pushed by implement step).

Phase 1 of create-pr: assembles PR body, calls gh pr create, writes pr.json.

Usage:
    python -m scripts.gh.build_pr --issue-id 42

Reads:  /workspace/{issue_id}/issue.json
        /workspace/{issue_id}/impl_result.json
        /workspace/{issue_id}/plan_result.json
        /workspace/{issue_id}/plan.md
Writes: /workspace/{issue_id}/pr.json

Exit codes: 0=success, 1=error
"""

import re
import sys

import click

from scripts.common.artifacts import read_json, read_text, write_json
from scripts.common.config import load_config
from scripts.common.shell import run_cmd as _run


def _create_github_pr(
    org: str,
    repo: str,
    issue: dict,
    plan_result: dict,
    impl_result: dict,
    branch: str,
    plan_md: str = "",
) -> dict:
    issue_id = issue["number"]
    title = f"[AI] #{issue_id}: {issue['title']}"
    body = "\n".join([
        f"Closes #{issue_id}",
        "",
        "## Summary",
        plan_result.get("summary", "AI-generated implementation"),
        "",
        "## Implementation Details",
        f"- Commits: {impl_result.get('commits', 'unknown')}",
        f"- Tests: {impl_result.get('tests_status', 'unknown')}",
        f"- Complexity: {plan_result.get('total_complexity', 'unknown')}",
        "",
        "_This PR was created by an AI agent._",
    ])

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

    if plan_md:
        comment_body = f"## AI Implementation Plan\n\n{plan_md[:60000]}"
        _run([
            "gh", "api",
            f"repos/{org}/{repo}/issues/{pr_number}/comments",
            "-f", f"body={comment_body}",
        ], check=False)

    return {"url": pr_url, "number": pr_number}


@click.command()
@click.option("--issue-id", required=True, help="GitHub issue number")
def main(issue_id: str) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO", "GH_TOKEN"])
    print(f"[build-pr] issue={issue_id} org={config['GH_ORG']} repo={config['GH_REPO']}", flush=True)

    existing = read_json(config, issue_id, "pr.json")
    if existing and existing.get("url"):
        print(f"[build-pr] PR already exists: {existing['url']}", flush=True)
        print(f"::add-job-context PRUrl::{existing['url']}")
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

    branch = impl_result.get("branch")
    if not branch:
        print("ERROR: impl_result.json missing 'branch' field", file=sys.stderr)
        sys.exit(1)

    org = config["GH_ORG"]
    repo = issue.get("repo") or config["GH_REPO"]
    plan_md = read_text(config, issue_id, "plan.md") or ""

    print(f"[build-pr] creating PR {branch} → main", flush=True)
    pr_info = _create_github_pr(org, repo, issue, plan_result, impl_result, branch, plan_md=plan_md)

    write_json(config, issue_id, "pr.json", {
        "url": pr_info["url"],
        "number": pr_info["number"],
        "branch": branch,
        "issue_id": issue_id,
    })

    print(f"[build-pr] PR created: {pr_info['url']}", flush=True)
    print(f"::add-job-context PRUrl::{pr_info['url']}")
    print(f"::add-job-context PRNumber::{pr_info['number']}")
    print(f"::add-job-context BranchName::{branch}")
    sys.exit(0)


if __name__ == "__main__":
    main()
