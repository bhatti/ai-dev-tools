"""Implement a plan: clone repo, apply changes, run tests, commit.

Usage:
    python -m scripts.gh.implement --issue-id 42

Required env: GH_ORG, GH_REPO
Reads:  /workspace/{issue_id}/issue.json
        /workspace/{issue_id}/plan.md
Writes: /workspace/{issue_id}/impl_result.json
        /workspace/{issue_id}/repo/  (cloned repo with changes)

Idempotent: reuses existing clone and branch if present.
Exit codes: 0=done, 2=blocked, 1=error/tests-failing
"""

import sys
from pathlib import Path

import click

from scripts.common.artifacts import read_json, read_text, write_json, write_log, write_text
from scripts.common.claude_runner import run_claude
from scripts.common.config import get_issue_dir, load_config
from scripts.common.git_utils import (
    clone_repo,
    commit_all,
    configure_git,
    create_branch,
    current_branch,
    detect_repo_url,
    get_commit_count,
    make_branch_name,
)


IMPLEMENT_PROMPT_TEMPLATE = """\
You are an expert software engineer implementing a GitHub issue.

## Issue #{issue_id}: {title}

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
@click.option("--issue-id", required=True, help="Issue number to implement")
def main(issue_id: str) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO"])

    # Idempotency check
    existing = read_json(config, issue_id, "impl_result.json")
    if existing and existing.get("status") == "DONE":
        print(f"Implementation already complete for issue #{issue_id}, skipping")
        sys.exit(0)

    issue = read_json(config, issue_id, "issue.json")
    if not issue:
        print(f"ERROR: /workspace/{issue_id}/issue.json not found", file=sys.stderr)
        sys.exit(1)

    plan = read_text(config, issue_id, "plan.md")
    if not plan:
        print(f"ERROR: /workspace/{issue_id}/plan.md not found", file=sys.stderr)
        sys.exit(1)

    org = config["GH_ORG"]
    repo = issue.get("repo") or config["GH_REPO"]
    issue_dir = get_issue_dir(config, issue_id)
    repo_dir = issue_dir / "repo"

    # Clone or reuse
    use_ssh = not config.get("GH_TOKEN") or config.get("USE_SSH", "0") == "1"
    ssh_key = config.get("SSH_PRIVATE_KEY", "")
    if config.get("GH_TOKEN") and not use_ssh:
        token = config["GH_TOKEN"]
        clone_url = f"https://x-access-token:{token}@github.com/{org}/{repo}.git"
        print(f"Cloning {org}/{repo} to {repo_dir}")
        clone_repo(clone_url, repo_dir)
    else:
        clone_url = detect_repo_url(org, repo, use_ssh=True)
        print(f"Cloning {org}/{repo} to {repo_dir}")
        clone_repo(clone_url, repo_dir, ssh_key=ssh_key)

    configure_git(repo_dir, config["GIT_USER_NAME"], config["GIT_USER_EMAIL"])

    # Create or reuse branch
    branch_file = issue_dir / "branch.txt"
    if branch_file.exists():
        branch = branch_file.read_text().strip()
    else:
        branch = make_branch_name(issue_id, issue["title"])
        branch_file.write_text(branch)

    print(f"Using branch: {branch}")
    create_branch(repo_dir, branch)

    prompt = IMPLEMENT_PROMPT_TEMPLATE.format(
        issue_id=issue_id,
        title=issue["title"],
        body=issue.get("body", "(no description)"),
        plan=plan,
    )

    model = config.get("AI_MODEL")
    max_turns = int(config.get("MAX_TURNS_IMPLEMENT", "100"))
    log_path = issue_dir / "logs" / "implement.log"

    print(f"Implementing with model={model}, max_turns={max_turns}")
    try:
        result = run_claude(prompt, working_dir=repo_dir, model=model, max_turns=max_turns, log_file=log_path)
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr)
        write_json(config, issue_id, "impl_result.json", {"status": "ERROR", "branch": branch, "reason": str(e)})
        sys.exit(1)

    # Catch-all commit for any uncommitted changes
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

    if commit_count == 0:
        print("WARNING: No commits made — implementation may be incomplete")

    print(f"Implementation complete: {commit_count} commits, status={result.status}")
    print(f"::set-output name=BranchName::{branch}")
    print(f"::set-output name=CommitCount::{commit_count}")
    sys.exit(0)


if __name__ == "__main__":
    main()
