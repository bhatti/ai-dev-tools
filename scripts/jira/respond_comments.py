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
    push_branch,
)
from scripts.common.shell import run_cmd as _run
from scripts.standup.slack_client import notify


def _ensure_repo_clone(config: dict, workspace: str, repo_name: str, branch: str, repo_dir: Path) -> None:
    http_token = config.get("BITBUCKET_TOKEN", "")
    ssh_key = config.get("SSH_PRIVATE_KEY", "")
    http_username = config.get("BITBUCKET_USERNAME", "x-token-auth")

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
) -> None:
    issue_dir = get_issue_dir(config, issue_id)
    http_token = config.get("BITBUCKET_TOKEN", "")
    http_username = config.get("BITBUCKET_USERNAME", "x-token-auth")
    author = comment.get("author", {}).get("nickname", "unknown")
    body = comment.get("content", {}).get("raw", "") or comment.get("body", "")
    comment_id = comment.get("id")

    max_turns = int(config.get("MAX_TURNS_FEEDBACK", "10"))
    prompt = f"""\
You are an AI agent responding to BitBucket PR review feedback.

## Comment from @{author}
{body}

## Instructions
1. Read CLAUDE.md or any repo-specific coding guidelines if they exist and follow them.
2. Analyze the feedback carefully.
3. Make the requested changes — edit the file directly. Keep changes minimal and focused.
4. Do NOT run tests or lint — just make the change and commit.
5. Commit with: "feedback: address comment from @{author}"
6. Output ONLY this JSON on the last line:
   {{"status":"DONE","commits":<N>,"summary":"<one sentence>"}}
   Or if you cannot address it:
   {{"status":"SKIPPED","reason":"<explanation>"}}
"""
    result = run_claude(
        prompt,
        working_dir=repo_dir,
        model=config.get("AI_MODEL"),
        max_turns=max_turns,
        log_file=issue_dir / "logs" / f"feedback_{comment_id}.log",
        system_prompt=SYSTEM_PROMPTS["respond"],
    )
    commit_all(repo_dir, f"feedback: address comment from @{author}")

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
    reply = f"Addressed feedback from @{author}. {summary}".strip()
    add_pr_comment(config, workspace, repo_name, pr_id, reply)


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
    pr_id = pr.get("id") or pr.get("url", "").rstrip("/").split("/")[-1]
    branch = pr["branch"]

    issue_dir = get_issue_dir(config, issue_id)
    repo_dir = issue_dir / "repo"

    if not ai_bot_comments:
        print(f"[respond-comments] no actionable comments — PR still open", flush=True)
        sys.exit(3)

    _ensure_repo_clone(config, workspace, repo_name, branch, repo_dir)

    for comment in ai_bot_comments:
        comment_id = comment.get("id")
        author = comment.get("author", {}).get("nickname", "unknown")
        print(f"  Responding to comment {comment_id} from @{author}", flush=True)
        try:
            _respond_to_comment(config, issue_id, workspace, repo_name, pr_id, comment, repo_dir, branch)
        except Exception as e:
            print(f"WARNING: failed to respond to comment {comment_id}: {e}", file=sys.stderr)

    notify(
        config,
        f"🔄 Addressed {len(ai_bot_comments)} review comment(s) on PR {pr_id} (issue {issue_id}): {pr.get('url', '')}",
    )
    print(f"[respond-comments] handled {len(ai_bot_comments)} comment(s)", flush=True)
    sys.exit(3)


if __name__ == "__main__":
    main()
