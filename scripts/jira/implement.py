"""Implement a Jira issue: clone BitBucket repo, apply changes, run tests, commit.

Usage:
    python -m scripts.jira.implement --issue-id PROJ-42

Required env: BITBUCKET_WORKSPACE, BITBUCKET_REPO (or from issue.json)
Reads:  /workspace/{issue_id}/issue.json
        /workspace/{issue_id}/plan.md
Writes: /workspace/{issue_id}/impl_result.json
        /workspace/{issue_id}/repo/

Idempotent: reuses existing clone and branch if present.
Exit codes: 0=done, 2=blocked, 1=error/tests-failing
"""

import sys

import click

from scripts.common.artifacts import read_json, read_text, write_json, write_log
from scripts.common.claude_runner import run_claude
from scripts.common.config import get_issue_dir, load_config
from scripts.common.git_utils import (
    clone_repo,
    commit_all,
    configure_git,
    create_branch,
    detect_bitbucket_url,
    get_commit_count,
    make_branch_name,
)


IMPLEMENT_PROMPT_TEMPLATE = """\
You are an expert software engineer implementing a Jira issue.

## {issue_key}: {title}

{body}

## Implementation Plan

{plan}

## Instructions

1. Read CLAUDE.md, .cursorrules, .windsurfrules, or any repo-specific coding guidelines if they exist — follow them strictly throughout.
2. Keep changes simple and robust: modify existing code rather than adding new layers. Avoid over-engineering.
3. Use the /ygs-implement skill if available, otherwise implement each task directly.
4. For each task:
   - Make actual file changes following the repo's existing patterns
   - Run tests to verify they pass
   - Commit with message: "task: <description>"
5. After all tasks, run the full test suite.
6. If tests fail, fix them before proceeding. Stop after 2 consecutive failed fix attempts.
7. Output ONLY this JSON on the last line:
   {{"status":"DONE","files_changed":["file1","file2"],"commits":<N>,"tests_status":"passing"}}
   Or if blocked:
   {{"status":"BLOCKED","reason":"<explanation>"}}
   Or if tests still failing:
   {{"status":"TESTS_FAILING","reason":"<explanation>","commits":<N>}}
"""


@click.command()
@click.option("--issue-id", required=True, help="Jira issue key (e.g. PROJ-42)")
def main(issue_id: str) -> None:
    config = load_config(required=[])

    existing = read_json(config, issue_id, "impl_result.json")
    if existing and existing.get("status") == "DONE":
        print(f"Implementation already complete for {issue_id}, skipping")
        existing_branch = existing.get("branch", "")
        if existing_branch:
            print(f"::set-output name=BranchName::{existing_branch}")
        sys.exit(0)

    issue = read_json(config, issue_id, "issue.json")
    if not issue:
        print(f"ERROR: /workspace/{issue_id}/issue.json not found", file=sys.stderr)
        sys.exit(1)

    plan = read_text(config, issue_id, "plan.md")
    if not plan:
        print(f"ERROR: /workspace/{issue_id}/plan.md not found", file=sys.stderr)
        sys.exit(1)

    # Repo info from issue.json (set by picker) or env fallback
    workspace = issue.get("bitbucket_workspace") or config.get("BITBUCKET_WORKSPACE", "")
    repo_name = issue.get("bitbucket_repo") or config.get("BITBUCKET_REPO", "")

    if not workspace or not repo_name:
        print("ERROR: BITBUCKET_WORKSPACE and BITBUCKET_REPO must be set", file=sys.stderr)
        sys.exit(1)

    issue_dir = get_issue_dir(config, issue_id)
    repo_dir = issue_dir / "repo"

    http_token = config.get("BITBUCKET_TOKEN", "")
    ssh_key = config.get("SSH_PRIVATE_KEY", "")

    if http_token:
        clone_url = detect_bitbucket_url(workspace, repo_name, use_ssh=False)
        http_username = config.get("BITBUCKET_USERNAME", "x-token-auth")
        print(f"Cloning {workspace}/{repo_name} via HTTPS token")
        clone_repo(clone_url, repo_dir, http_token=http_token, http_username=http_username)
    else:
        clone_url = detect_bitbucket_url(workspace, repo_name, use_ssh=True)
        print(f"Cloning {workspace}/{repo_name} via SSH")
        clone_repo(clone_url, repo_dir, ssh_key=ssh_key)
    configure_git(repo_dir, config["GIT_USER_NAME"], config["GIT_USER_EMAIL"])

    branch_file = issue_dir / "branch.txt"
    if branch_file.exists():
        branch = branch_file.read_text().strip()
    else:
        branch = make_branch_name(issue_id.replace("/", "-"), issue["title"])
        branch_file.write_text(branch)

    print(f"Using branch: {branch}")
    create_branch(repo_dir, branch)

    prompt = IMPLEMENT_PROMPT_TEMPLATE.format(
        issue_key=issue_id,
        title=issue["title"],
        body=issue.get("body", "(no description)"),
        plan=plan,
    )

    try:
        result = run_claude(
            prompt,
            working_dir=repo_dir,
            model=config.get("AI_MODEL"),
            max_turns=int(config.get("MAX_TURNS_IMPLEMENT", "100")),
            log_file=issue_dir / "logs" / "implement.log",
        )
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr)
        write_json(config, issue_id, "impl_result.json", {"status": "ERROR", "branch": branch, "reason": str(e)})
        sys.exit(1)

    commit_all(repo_dir, "implement: changes from AI agent")
    commit_count = get_commit_count(repo_dir)

    result_data = result.status_json or {"status": result.status}
    result_data["commits"] = result_data.get("commits", commit_count)
    result_data["branch"] = branch

    write_json(config, issue_id, "impl_result.json", result_data)
    write_log(config, issue_id, "implement", result.output)

    if result.status == "BLOCKED":
        print(f"Implementation blocked: {result.status_json.get('reason', 'unknown')}")
        sys.exit(2)

    if result.status == "TESTS_FAILING":
        print(f"Tests failing: {result.status_json.get('reason', 'unknown')}")
        sys.exit(1)

    print(f"Implementation complete: {commit_count} commits")
    print(f"::set-output name=BranchName::{branch}")
    print(f"::set-output name=CommitCount::{commit_count}")
    sys.exit(0)


if __name__ == "__main__":
    main()
