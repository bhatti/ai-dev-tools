"""Clone repo (if needed) and respond to actionable BitBucket PR comments.

Phase 3 of poll-pr: reads pending_comments.json, clones the feature branch,
calls Claude per comment, pushes, posts reply. Exits 3 if PR is still open.

Usage:
    python -m scripts.jira.respond_comments --issue-id PROJ-42

Reads:  /workspace/{issue_id}/pr.json
        /workspace/{issue_id}/poll_state.json
        /workspace/{issue_id}/pending_comments.json
Writes: /workspace/{issue_id}/logs/feedback_<id>.log  (per comment)

Exit codes: 0=terminal/done, 3=PR still open, 1=error
"""

import sys
from pathlib import Path

import click

from scripts.common.artifacts import read_json, write_json
from scripts.common.bitbucket_api import add_pr_comment
from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS
from scripts.common.config import get_issue_dir, load_config, validate_claude_config
from scripts.common.git_utils import (
    clone_repo,
    commit_all,
    configure_git,
    create_branch,
    detect_bitbucket_url,
    get_bitbucket_git_username,
    push_branch,
)
from scripts.common.shell import run_cmd as _run
from scripts.standup.slack_client import notify


def _ensure_repo_clone(config: dict, workspace: str, repo_name: str, branch: str, repo_dir: Path) -> None:
    http_token = config.get("BITBUCKET_TOKEN", "")
    ssh_key = config.get("SSH_PRIVATE_KEY", "")
    http_username = get_bitbucket_git_username(config)

    clone_url = detect_bitbucket_url(workspace, repo_name, use_ssh=not http_token)

    if not (repo_dir.exists() and (repo_dir / ".git").exists()):
        print(f"[respond-comments] cloning {workspace}/{repo_name} branch={branch}", flush=True)
        if http_token:
            clone_repo(clone_url, repo_dir, http_token=http_token, http_username=http_username)
        else:
            clone_repo(clone_url, repo_dir, ssh_key=ssh_key)
        configure_git(
            repo_dir,
            config.get("GIT_USER_NAME", "AI Agent"),
            config.get("GIT_USER_EMAIL", "ai-agent@noreply.local"),
        )

    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    fetch = _run(["git", "-C", str(repo_dir), "fetch", "--depth", "100", "origin", refspec], check=False)
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch failed for branch {branch}: {fetch.stderr.strip() or fetch.stdout.strip()}")

    create_branch(repo_dir, branch)


def _respond_to_comment(
    config: dict,
    issue_id: str,
    workspace: str,
    repo_name: str,
    pr_id: int,
    comment: dict,
    repo_dir: Path,
    branch: str,
) -> bool:
    issue_dir = get_issue_dir(config, issue_id)
    http_token = config.get("BITBUCKET_TOKEN", "")
    http_username = get_bitbucket_git_username(config)
    author = comment.get("author", {}).get("nickname", "unknown")
    body = comment.get("content", {}).get("raw", "") or comment.get("body", "")
    comment_id = comment.get("id")
    if not comment_id:
        raise RuntimeError(f"comment is missing 'id' field — cannot process: {comment}")

    max_turns = int(config.get("MAX_TURNS_FEEDBACK", "10"))
    prompt = f"""\
You are an AI agent responding to BitBucket PR review feedback on a skills/documentation PR.

## Comment from @{author}
{body}

## Instructions
1. Read CLAUDE.md and any repo-specific guidelines.
2. Analyze the feedback carefully.
3. This PR contains ONLY skill files (.claude/skills/**), ADR documents (docs/adr/**), and
   documentation (README, docs/**). Make changes ONLY to those files.
   NEVER modify production source code, test files, or application code — even if the
   reviewer asks for it. If the comment requests a code change, respond with SKIPPED and
   explain that code changes are out of scope for this PR; suggest the appropriate repo instead.
4. Do NOT run git commands — do not commit, stage, or push.
5. Output ONLY this JSON on the last line:
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
        print(f"[respond-comments] comment {comment_id}: SKIPPED — {reason}", flush=True)
        # Post the explanation so the dedup marker is recorded and this comment isn't retried
        reply = (
            f"Note: this comment was reviewed but does not require code changes — {reason}\n\n"
            f"<!-- replied-to: {comment_id} -->"
        ).strip()
        add_pr_comment(config, workspace, repo_name, pr_id, reply, parent_id=comment_id)
        return False

    committed = commit_all(repo_dir, f"feedback: address comment from @{author}")
    if not committed:
        print(f"[respond-comments] comment {comment_id}: DONE reported but no file changes found", file=sys.stderr, flush=True)
        raise RuntimeError(f"Claude reported DONE for comment {comment_id} but made no file changes")

    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    fetch_result = _run(["git", "-C", str(repo_dir), "fetch", "--depth", "100", "origin", refspec], check=False)
    if fetch_result.returncode != 0:
        print(f"WARNING: pre-push fetch failed: {fetch_result.stderr.strip()}", file=sys.stderr)

    if http_token:
        push_url = detect_bitbucket_url(workspace, repo_name, use_ssh=False)
        push_branch(repo_dir, branch, force_with_lease=True,
                    http_token=http_token, http_username=http_username, url=push_url)
    else:
        push_branch(repo_dir, branch, force_with_lease=True)

    summary = (result.status_json or {}).get("summary", "")
    reply = (
        f"Addressed feedback from @{author}: {summary}\n\n"
        f"<!-- replied-to: {comment_id} -->"
    ).strip()
    add_pr_comment(config, workspace, repo_name, pr_id, reply, parent_id=comment_id)
    return True


@click.command()
@click.option("--issue-id", required=True, help="Jira issue key (e.g. PROJ-42)")
def main(issue_id: str) -> None:
    config = load_config(required=["BITBUCKET_USERNAME", "BITBUCKET_TOKEN"])
    validate_claude_config(config)
    print(f"[respond-comments] issue={issue_id}", flush=True)

    poll_state = read_json(config, issue_id, "poll_state.json")
    if poll_state and poll_state.get("terminal"):
        print("[respond-comments] PR is terminal — skipping", flush=True)
        sys.exit(0)

    pending = read_json(config, issue_id, "pending_comments.json") or {"comments": []}
    ai_bot_comments = pending.get("comments", [])

    pr = read_json(config, issue_id, "pr.json")
    if not pr:
        print("ERROR: pr.json not found", file=sys.stderr)
        sys.exit(1)

    workspace = pr.get("workspace") or config.get("BITBUCKET_WORKSPACE", "")
    repo_name = pr.get("repo") or config.get("BITBUCKET_REPO", "")
    pr_id = (pr.get("number") or pr.get("id") or pr.get("url", "").rstrip("/").split("/")[-1])
    branch = pr["branch"]

    issue_dir = get_issue_dir(config, issue_id)
    repo_dir = issue_dir / "repo"

    if not ai_bot_comments:
        print(f"[respond-comments] no actionable comments — PR still open", flush=True)
        sys.exit(3)

    _ensure_repo_clone(config, workspace, repo_name, branch, repo_dir)

    addressed = 0
    for comment in ai_bot_comments:
        comment_id = comment.get("id")
        author = comment.get("author", {}).get("nickname", "unknown")
        print(f"  Responding to comment {comment_id} from @{author}", flush=True)
        if _respond_to_comment(config, issue_id, workspace, repo_name, pr_id, comment, repo_dir, branch):
            addressed += 1

    print(f"[respond-comments] handled {len(ai_bot_comments)} comment(s), committed changes for {addressed}", flush=True)
    if addressed > 0:
        notify(
            config,
            f"🔄 Addressed {addressed} review comment(s) on PR {pr_id} (issue {issue_id}): {pr.get('url', '')}",
        )
    sys.exit(3)


if __name__ == "__main__":
    main()
