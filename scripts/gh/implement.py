"""Implement a plan: clone repo, apply changes, run tests, commit.

Usage:
    python -m scripts.gh.implement --issue-id 42

Required env: GH_ORG, GH_REPO
Reads:  /workspace/issue.json
        /workspace/plan.md
Writes: /workspace/impl_result.json
        /workspace/repo/  (cloned repo with changes)

Idempotent: reuses existing clone and branch if present.
Exit codes: 0=done, 2=blocked, 1=error/tests-failing
"""

import sys
from pathlib import Path

import click

from scripts.common.artifacts import read_json, read_text, write_json, write_log, write_text
from scripts.common.claude_runner import run_claude
from scripts.common.config import get_issue_dir, load_config, validate_claude_config
from scripts.common.git_utils import (
    clone_repo,
    commit_all,
    configure_git,
    create_branch,
    current_branch,
    detect_repo_url,
    get_commit_count,
    make_branch_name,
    push_branch,
)


IMPLEMENT_PROMPT_TEMPLATE = """\
You are an expert software engineer implementing a GitHub issue.

## Issue #{issue_id}: {title}

{body}

## Implementation Plan

{plan}

## Instructions

1. Read CLAUDE.md, .cursorrules, .windsurfrules, or any repo-specific coding guidelines if they exist — follow them strictly. Never deviate from the existing language, style, and patterns of the repository.
2. Before writing any implementation code, check `.claude/skills/` for relevant skills. If the plan names a skill, invoke it via the Skill tool rather than reimplementing its steps.
3. Search for existing utilities in `utils/`, `shared/`, `common/` dirs before adding new abstractions. Prefer reusing what exists.
4. Use TDD for non-trivial changes:
   a. Write the failing test first and confirm it fails.
   b. Implement the code.
   c. Confirm the test passes.
5. For each plan task:
   - Make targeted file changes following the repo's existing patterns.
   - Do NOT modify files unrelated to the task.
   - Commit with message: "task: <description>"
6. After all tasks, run the full test suite. Do NOT run lint or eslint — only the test suite.
7. If tests fail, iterate on the fix. Stop after 2 consecutive failed fix attempts.
8. After tests pass, do a cleanup pass: remove any unused variables, imports, or dead code you introduced.
9. Output ONLY this JSON on the last line (no text after it):
   {{"status":"DONE","files_changed":["file1","file2"],"commits":<N>,"tests_status":"passing","summary":"<one sentence>"}}
   Or if blocked / requirements unclear:
   {{"status":"CANNOT_IMPLEMENT","reason":"<explanation>"}}
   Or if tests still failing after retries:
   {{"status":"TESTS_FAILING","reason":"<explanation>","commits":<N>}}

IMPORTANT: Always write the JSON result on the last line regardless of outcome. This is the handoff contract for the next step.
"""


@click.command()
@click.option("--issue-id", required=True, help="Issue number to implement")
def main(issue_id: str) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO"])
    validate_claude_config(config)
    print(f"[implement] issue={issue_id} org={config['GH_ORG']} repo={config['GH_REPO']}", flush=True)

    # Idempotency check
    existing = read_json(config, issue_id, "impl_result.json")
    if existing and existing.get("status") == "DONE":
        print(f"Implementation already complete for issue #{issue_id}, skipping")
        sys.exit(0)

    issue = read_json(config, issue_id, "issue.json")
    if not issue:
        print(f"ERROR: {get_issue_dir(config, issue_id)}/issue.json not found", file=sys.stderr)
        sys.exit(1)

    plan = read_text(config, issue_id, "plan.md")
    if not plan:
        print(f"ERROR: {get_issue_dir(config, issue_id)}/plan.md not found", file=sys.stderr)
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

    # Push here so create-pr only needs the GitHub API, not the local repo clone.
    token = config.get("GH_TOKEN", "")
    org = config["GH_ORG"]
    repo = issue.get("repo") or config["GH_REPO"]
    push_branch(repo_dir, branch, http_token=token, http_username="x-access-token",
                url=f"https://github.com/{org}/{repo}.git" if token else "")

    commit_count = get_commit_count(repo_dir)
    result_data = result.status_json or {"status": result.status}
    result_data["commits"] = result_data.get("commits", commit_count)
    result_data["branch"] = branch

    write_json(config, issue_id, "impl_result.json", result_data)
    write_log(config, issue_id, "implement", result.output)

    sj = result.status_json or {}

    if result.status == "MAX_TURNS_REACHED":
        print(f"Max turns reached — partial implementation pushed to branch '{branch}' ({commit_count} commits)")
        print(f"::set-output name=BranchName::{branch}")
        print(f"::set-output name=CommitCount::{commit_count}")
        sys.exit(2)

    if result.status in ("BLOCKED", "CANNOT_IMPLEMENT"):
        print(f"Implementation blocked: {sj.get('reason', 'unknown')}")
        sys.exit(2)

    if result.status == "TESTS_FAILING":
        print(f"Tests failing: {sj.get('reason', 'unknown')}")
        sys.exit(1)

    if result.status not in ("DONE",):
        print(f"WARNING: unexpected status '{result.status}' — treating as incomplete")
        if commit_count == 0:
            write_json(config, issue_id, "impl_result.json", {"status": "ERROR", "branch": branch, "reason": f"unexpected status: {result.status}"})
            sys.exit(1)

    if commit_count == 0:
        print("WARNING: No commits made — implementation may be incomplete")

    print(f"Implementation complete: {commit_count} commits, status={result.status}")
    print(f"::set-output name=BranchName::{branch}")
    print(f"::set-output name=CommitCount::{commit_count}")
    sys.exit(0)


if __name__ == "__main__":
    main()
