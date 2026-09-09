"""Clone repo (if needed) and respond to actionable GitHub PR comments.

Phase 3 of poll-pr: reads pending_comments.json, clones the feature branch,
calls Claude per comment, pushes, posts reply. Exits 3 if PR is still open.

Usage:
    python -m scripts.gh.respond_comments --issue-id 42

Reads:  /workspace/{issue_id}/pr.json
        /workspace/{issue_id}/poll_state.json
        /workspace/{issue_id}/pending_comments.json
Writes: /workspace/{issue_id}/logs/feedback_<id>.log  (per comment)

Exit codes: 0=terminal/done, 3=PR still open, 1=error
"""

import sys
from pathlib import Path

import click

from scripts.common.artifacts import read_json
from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS
from scripts.common.config import get_issue_dir, load_config, validate_claude_config
from scripts.common.git_utils import (
    clone_repo,
    commit_all,
    configure_git,
    create_branch,
    push_branch,
)
from scripts.common.shell import run_cmd as _run
from scripts.standup.slack_client import notify


def _ensure_repo_clone(config: dict, org: str, repo: str, branch: str, repo_dir: Path) -> None:
    token = config.get("GH_TOKEN", "")
    clone_url = (
        f"https://x-access-token:{token}@github.com/{org}/{repo}.git"
        if token else
        f"git@github.com:{org}/{repo}.git"
    )

    if not (repo_dir.exists() and (repo_dir / ".git").exists()):
        print(f"[respond-comments] cloning {org}/{repo} branch={branch}", flush=True)
        clone_repo(clone_url, repo_dir)
        configure_git(
            repo_dir,
            config.get("GIT_USER_NAME", "AI Agent"),
            config.get("GIT_USER_EMAIL", "ai-agent@noreply.local"),
        )

    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    fetch = _run(["git", "-C", str(repo_dir), "fetch", "--depth", "100", "origin", refspec], check=False)
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch failed for branch {branch}: {fetch.stderr.strip() or fetch.stdout.strip()}")

    if token:
        _run(["git", "-C", str(repo_dir), "remote", "set-url", "origin", clone_url], check=False)

    create_branch(repo_dir, branch)


def _respond_to_comment(
    config: dict,
    issue_id: str,
    org: str,
    repo: str,
    pr_number: int,
    comment: dict,
    repo_dir: Path,
    branch: str,
) -> bool:
    issue_dir = get_issue_dir(config, issue_id)
    max_turns = int(config.get("MAX_TURNS_FEEDBACK", "10"))
    user = comment["user"]
    body = comment.get("body", "")
    comment_id = comment["id"]
    file_ctx = f"\n## File: {comment['path']}" if comment.get("path") else ""

    prompt = f"""\
You are an AI agent responding to a PR review comment.

## Comment from @{user}
{body}{file_ctx}

## Instructions
1. Read CLAUDE.md or any repo-specific coding guidelines and follow them strictly.
2. Analyze the feedback carefully and understand what change is needed.
3. Make the requested changes — edit the file(s) directly. Keep changes minimal and focused.
4. If you changed code files (not markdown/docs/skills), check CLAUDE.md or Makefile for a fast lint/type-check command and run it. Do NOT run full test suites.
5. Do NOT run git commands — do not commit, stage, or push.
6. Output ONLY this JSON on the last line:
   {{"status":"DONE","summary":"<one sentence describing exactly what was changed and why>"}}
   Or if you cannot address it:
   {{"status":"SKIPPED","reason":"<explanation of why the comment cannot be acted on>"}}
"""
    result = run_claude(
        prompt,
        working_dir=repo_dir,
        model=config.get("AI_MODEL"),
        max_turns=max_turns,
        log_file=issue_dir / "logs" / f"feedback_{comment_id}.log",
        system_prompt=SYSTEM_PROMPTS["respond"],
    )

    status = (result.status_json or {}).get("status", "")
    if status == "SKIPPED":
        reason = (result.status_json or {}).get("reason", "unknown")
        print(f"[respond-comments] comment {comment_id}: SKIPPED — {reason}", file=sys.stderr, flush=True)
        raise RuntimeError(f"Claude could not address comment {comment_id}: {reason}")

    committed = commit_all(repo_dir, f"feedback: address comment from @{user}")
    if not committed:
        print(f"[respond-comments] comment {comment_id}: DONE reported but no file changes found", file=sys.stderr, flush=True)
        raise RuntimeError(f"Claude reported DONE for comment {comment_id} but made no file changes")

    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    fetch_result = _run(["git", "-C", str(repo_dir), "fetch", "--depth", "100", "origin", refspec], check=False)
    if fetch_result.returncode != 0:
        print(f"WARNING: pre-push fetch failed: {fetch_result.stderr.strip()}", file=sys.stderr)

    push_branch(repo_dir, branch, force_with_lease=True)

    summary = (result.status_json or {}).get("summary", "")
    reply_body = f"Addressed feedback from @{user}: {summary}".strip().rstrip(":")
    reply = _run([
        "gh", "api", f"repos/{org}/{repo}/issues/{pr_number}/comments",
        "-f", f"body={reply_body}",
    ], check=False)
    if reply.returncode != 0:
        raise RuntimeError(f"Failed to post reply on PR #{pr_number}: {reply.stderr.strip()}")
    return True


@click.command()
@click.option("--issue-id", required=True, help="GitHub issue number")
def main(issue_id: str) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO", "GH_TOKEN"])
    validate_claude_config(config)
    print(f"[respond-comments] issue={issue_id}", flush=True)

    poll_state = read_json(config, issue_id, "poll_state.json")
    if poll_state and poll_state.get("terminal"):
        print("[respond-comments] PR is terminal — skipping", flush=True)
        sys.exit(0)

    pending = read_json(config, issue_id, "pending_comments.json") or {"comments": []}
    actionable = pending.get("comments", [])

    pr = read_json(config, issue_id, "pr.json")
    if not pr:
        print("ERROR: pr.json not found", file=sys.stderr)
        sys.exit(1)

    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    pr_number = int(pr["number"])
    branch = pr["branch"]

    issue_dir = get_issue_dir(config, issue_id)
    repo_dir = issue_dir / "repo"

    if not actionable:
        print(f"[respond-comments] no actionable comments — PR still open", flush=True)
        sys.exit(3)

    _ensure_repo_clone(config, org, repo, branch, repo_dir)

    addressed = 0
    for comment in actionable:
        comment_id = comment["id"]
        user = comment.get("user", "unknown")
        print(f"  Responding to comment #{comment_id} from @{user}", flush=True)
        try:
            if _respond_to_comment(config, issue_id, org, repo, pr_number, comment, repo_dir, branch):
                addressed += 1
        except Exception as e:
            print(f"WARNING: failed to respond to comment #{comment_id}: {e}", file=sys.stderr)

    print(f"[respond-comments] handled {len(actionable)} comment(s), committed changes for {addressed}", flush=True)
    if addressed > 0:
        notify(
            config,
            f"🔄 Addressed {addressed} review comment(s) on PR #{pr_number} (issue #{issue_id}): {pr.get('url', '')}",
        )
    sys.exit(3)


if __name__ == "__main__":
    main()
