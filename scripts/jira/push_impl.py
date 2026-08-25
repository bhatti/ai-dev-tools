"""Commit remaining changes and push the implementation branch for a Jira issue.

Usage:
    python -m scripts.jira.push_impl --issue-id PROJ-42

Reads:  /workspace/{issue_id}/branch.txt
        /workspace/{issue_id}/impl_run_result.json
Writes: /workspace/{issue_id}/impl_result.json

Exit codes: 0=done, 2=blocked/max-turns, 1=error/tests-failing
"""

import sys

import click

from scripts.common.artifacts import read_json, write_json
from scripts.common.config import get_issue_dir, load_config
from scripts.common.git_utils import (
    commit_all,
    detect_bitbucket_url,
    get_commit_count,
    push_branch,
)


@click.command()
@click.option("--issue-id", required=True, help="Jira issue key (e.g. PROJ-42)")
def main(issue_id: str) -> None:
    config = load_config(required=[])
    print(f"[push-impl] issue={issue_id}", flush=True)

    issue_dir = get_issue_dir(config, issue_id)

    branch_file = issue_dir / "branch.txt"
    if not branch_file.exists():
        print(f"ERROR: {branch_file} not found — run clone-repo first", file=sys.stderr)
        sys.exit(1)
    branch = branch_file.read_text().strip()

    run_result = read_json(config, issue_id, "impl_run_result.json")
    if not run_result:
        print(f"ERROR: impl_run_result.json not found — run run-implement first", file=sys.stderr)
        sys.exit(1)

    issue = read_json(config, issue_id, "issue.json")
    workspace = (issue or {}).get("bitbucket_workspace") or config.get("BITBUCKET_WORKSPACE", "")
    repo_name = (issue or {}).get("bitbucket_repo") or config.get("BITBUCKET_REPO", "")

    repo_dir = issue_dir / "repo"
    http_token = config.get("BITBUCKET_TOKEN", "")
    http_username = config.get("BITBUCKET_USERNAME", "x-token-auth")
    base_branch = config.get("BASE_BRANCH", "main")

    commit_all(repo_dir, "implement: changes from AI agent")

    push_failed = False
    try:
        if http_token and workspace and repo_name:
            push_url = detect_bitbucket_url(workspace, repo_name, use_ssh=False)
            push_branch(repo_dir, branch, http_token=http_token, http_username=http_username, url=push_url)
        else:
            push_branch(repo_dir, branch)
    except Exception as e:
        print(f"WARNING: push failed (partial work preserved locally): {e}", file=sys.stderr)
        push_failed = True

    commit_count = get_commit_count(repo_dir, base_branch=base_branch)
    result_data = dict(run_result)
    result_data["commits"] = result_data.get("commits", commit_count)
    result_data["branch"] = branch

    write_json(config, issue_id, "impl_result.json", result_data)

    status = run_result.get("status", "")
    print(f"[push-impl] status={status} commits={commit_count} branch={branch}", flush=True)
    print(f"::add-job-context BranchName::{branch}")
    print(f"::add-job-context CommitCount::{commit_count}")

    if push_failed:
        sys.exit(1)

    if status in ("MAX_TURNS_REACHED", "BLOCKED", "CANNOT_IMPLEMENT"):
        sys.exit(2)
    if status == "TESTS_FAILING":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
