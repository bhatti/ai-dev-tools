"""Commit remaining changes and push the implementation branch for a GitHub issue.

Usage:
    python -m scripts.gh.push_impl --issue-id 42

Reads:  /workspace/{issue_id}/branch.txt
        /workspace/{issue_id}/impl_run_result.json
        /workspace/{issue_id}/issue.json
Writes: /workspace/{issue_id}/impl_result.json

Exit codes: 0=done, 2=blocked/max-turns, 1=error/tests-failing
"""

import sys

import click

from scripts.common.artifacts import read_json, write_json
from scripts.common.config import get_issue_dir, load_config
from scripts.common.git_utils import commit_all, get_commit_count, push_branch


@click.command()
@click.option("--issue-id", required=True, help="GitHub issue number")
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
        print("ERROR: impl_run_result.json not found — run run-implement first", file=sys.stderr)
        sys.exit(1)

    issue = read_json(config, issue_id, "issue.json")
    repo_dir = issue_dir / "repo"
    base_branch = config.get("BASE_BRANCH", "main")

    commit_all(repo_dir, "implement: changes from AI agent")

    token = config.get("GH_TOKEN", "")
    org = config.get("GH_ORG", "")
    repo_name = (issue or {}).get("repo") or config.get("GH_REPO", "")

    push_failed = False
    try:
        if token and org and repo_name:
            push_branch(
                repo_dir,
                branch,
                http_token=token,
                http_username="x-access-token",
                url=f"https://github.com/{org}/{repo_name}.git",
            )
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
