"""Push branch and create a BitBucket Pull Request via REST API.

Phase 1 of create-pr: assembles PR body and calls the BitBucket API.
Writes pr.json on success.

Usage:
    python -m scripts.jira.build_pr --issue-id PROJ-42

Reads:  /workspace/{issue_id}/issue.json
        /workspace/{issue_id}/impl_result.json
        /workspace/{issue_id}/plan_result.json
Writes: /workspace/{issue_id}/pr.json

Exit codes: 0=success, 1=error
"""

import sys

import click

from scripts.common.artifacts import read_json, write_json
from scripts.common.bitbucket_api import create_pr as bb_create_pr
from scripts.common.config import load_config


@click.command()
@click.option("--issue-id", required=True, help="Jira issue key (e.g. PROJ-42)")
def main(issue_id: str) -> None:
    config = load_config(required=["BITBUCKET_USERNAME", "BITBUCKET_TOKEN"])
    print(f"[build-pr] issue={issue_id}", flush=True)

    existing = read_json(config, issue_id, "pr.json")
    if existing and existing.get("url"):
        print(f"[build-pr] PR already exists: {existing['url']}", flush=True)
        print(f"::add-job-context PRUrl::{existing['url']}")
        sys.exit(0)

    issue = read_json(config, issue_id, "issue.json")
    impl_result = read_json(config, issue_id, "impl_result.json")
    plan_result = read_json(config, issue_id, "plan_result.json") or {}

    if not issue or not impl_result:
        print("ERROR: Missing issue.json or impl_result.json", file=sys.stderr)
        sys.exit(1)
    if impl_result.get("status") != "DONE":
        print(f"ERROR: impl_result status={impl_result.get('status')}: {impl_result.get('reason', '')}", file=sys.stderr)
        sys.exit(1)

    workspace = issue.get("bitbucket_workspace") or config.get("BITBUCKET_WORKSPACE", "")
    repo_name = issue.get("bitbucket_repo") or config.get("BITBUCKET_REPO", "")
    base_branch = issue.get("base_branch", "main")

    if not workspace or not repo_name:
        print("ERROR: BITBUCKET_WORKSPACE and BITBUCKET_REPO must be set", file=sys.stderr)
        sys.exit(1)

    branch = impl_result.get("branch")
    if not branch:
        print("ERROR: impl_result.json missing 'branch' field", file=sys.stderr)
        sys.exit(1)

    title = f"[AI] {issue_id}: {issue['title']}"
    description = "\n".join([
        f"Jira: {issue.get('url', issue_id)}",
        "",
        plan_result.get("summary", "AI-generated implementation"),
        "",
        f"Commits: {impl_result.get('commits', 'unknown')}",
        f"Tests: {impl_result.get('tests_status', 'unknown')}",
        "",
        "_This PR was created by an AI agent._",
    ])

    print(f"[build-pr] creating BitBucket PR: {workspace}/{repo_name} {branch} → {base_branch}", flush=True)
    pr = bb_create_pr(
        config,
        workspace=workspace,
        repo=repo_name,
        title=title,
        description=description,
        source_branch=branch,
        dest_branch=base_branch,
    )
    if not pr:
        print("ERROR: BitBucket PR creation failed", file=sys.stderr)
        sys.exit(1)

    pr_url = pr.get("links", {}).get("html", {}).get("href", "")
    pr_id = pr.get("id")

    write_json(config, issue_id, "pr.json", {
        "url": pr_url,
        "id": pr_id,
        "branch": branch,
        "workspace": workspace,
        "repo": repo_name,
        "issue_id": issue_id,
    })

    print(f"[build-pr] PR created: {pr_url}", flush=True)
    print(f"::add-job-context PRUrl::{pr_url}")
    print(f"::add-job-context PRId::{pr_id}")
    print(f"::add-job-context BranchName::{branch}")
    sys.exit(0)


if __name__ == "__main__":
    main()
